"""大华 RPAC-256 加解密协议（RSA + AES 混合加密）。

协议流程：
1. Security.getEncryptInfo 获取 RSA 公钥 (N, E)
2. 客户端生成随机 32 字节 AES 密钥
3. RSA 加密 AES 密钥 → salt (hex 编码)
4. AES-256 加密 JSON 数据 → content (base64 编码)
5. 设备解密 salt 得到 AES 密钥，再解密 content 得到明文
6. 响应 content 同样用 AES 加密，客户端用同一密钥解密
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.asymmetric import rsa, padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def _hex_to_int(hex_str: str) -> int:
    return int(hex_str, 16)


def _zero_pad(data: bytes, block_size: int = 16) -> bytes:
    """Zero padding: 补零到 block_size 的整数倍。"""
    remainder = len(data) % block_size
    if remainder == 0:
        return data + b"\x00" * block_size  # 始终补一个完整块
    return data + b"\x00" * (block_size - remainder)


def _zero_unpad(data: bytes) -> bytes:
    """Zero unpadding: 去掉尾部零字节。"""
    return data.rstrip(b"\x00")


def _pkcs7_pad(data: bytes, block_size: int = 128) -> bytes:
    padder = sym_padding.PKCS7(block_size).padder()
    return padder.update(data) + padder.finalize()


def _pkcs7_unpad(data: bytes, block_size: int = 128) -> bytes:
    unpadder = sym_padding.PKCS7(block_size).unpadder()
    return unpadder.update(data) + unpadder.finalize()


class RPACEncoder:
    """RPAC-256 编码器：RSA + AES 混合加密。

    可配置参数：
    - rsa_encrypt_iv: True=RSA加密(key+iv), False=RSA只加密key
    - aes_mode: "cbc" 或 "ecb"
    - aes_padding: "zero" 或 "pkcs7"
    - rsa_padding: "pkcs1v15" 或 "oaep"
    """

    def __init__(
        self,
        rsa_n: int,
        rsa_e: int,
        *,
        rsa_encrypt_iv: bool = True,
        aes_mode: str = "cbc",
        aes_padding: str = "pkcs7",
        rsa_padding: str = "pkcs1v15",
    ) -> None:
        self._n = rsa_n
        self._e = rsa_e
        self.rsa_encrypt_iv = rsa_encrypt_iv
        self.aes_mode = aes_mode
        self.aes_padding = aes_padding
        self.rsa_padding = rsa_padding
        self._public_key = rsa.RSAPublicNumbers(e=rsa_e, n=rsa_n).public_key(default_backend())
        self._key_size = (rsa_n.bit_length() + 7) // 8

    @classmethod
    def from_pub_string(cls, pub: str, **kwargs: Any) -> "RPACEncoder":
        """从 'N:hex,E:hex' 格式创建。"""
        parts = pub.split(",")
        n_hex = ""
        e_hex = ""
        for part in parts:
            if part.startswith("N:"):
                n_hex = part[2:]
            elif part.startswith("E:"):
                e_hex = part[2:]
        if not n_hex or not e_hex:
            raise ValueError(f"无法解析公钥: {pub[:80]}")
        return cls(_hex_to_int(n_hex), _hex_to_int(e_hex), **kwargs)

    def _rsa_encrypt(self, data: bytes) -> bytes:
        if self.rsa_padding == "oaep":
            return self._public_key.encrypt(data, asym_padding.OAEP(
                mgf=asym_padding.MGF1(algorithm=__import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256()),
                algorithm=__import__("cryptography.hazmat.primitives.hashes", fromlist=["SHA256"]).SHA256(),
                label=None,
            ))
        return self._public_key.encrypt(data, asym_padding.PKCS1v15())

    def _aes_encrypt(self, key: bytes, iv: bytes, plaintext: bytes) -> bytes:
        if self.aes_padding == "zero":
            padded = _zero_pad(plaintext)
        else:
            padded = _pkcs7_pad(plaintext)

        if self.aes_mode == "ecb":
            cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        else:
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def _aes_decrypt(self, key: bytes, iv: bytes, ciphertext: bytes) -> bytes:
        if self.aes_mode == "ecb":
            cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        else:
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        decryptor = cipher.decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()

        if self.aes_padding == "zero":
            return _zero_unpad(padded)
        return _pkcs7_unpad(padded)

    def encode_and_get_decryptor(self, data: dict[str, Any]) -> tuple[str, str, bytes, bytes]:
        """加密请求并返回解密所需信息。

        返回 (salt_hex, content_b64, aes_key, iv)。
        """
        aes_key = os.urandom(32)

        if self.aes_mode == "ecb":
            iv = b""
            encrypted_key = self._rsa_encrypt(aes_key)
        elif self.rsa_encrypt_iv:
            iv = os.urandom(16)
            encrypted_key = self._rsa_encrypt(aes_key + iv)
        else:
            iv = b"\x00" * 16
            encrypted_key = self._rsa_encrypt(aes_key)

        salt = encrypted_key.hex()

        plaintext = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        encrypted_data = self._aes_encrypt(aes_key, iv, plaintext)
        content = base64.b64encode(encrypted_data).decode("ascii")

        return salt, content, aes_key, iv

    def decrypt_response(self, content_b64: str, aes_key: bytes, iv: bytes) -> dict[str, Any]:
        ciphertext = base64.b64decode(content_b64)
        plaintext = self._aes_decrypt(aes_key, iv, ciphertext)
        return json.loads(plaintext.decode("utf-8"))

    def describe(self) -> str:
        return f"RSA({'key+iv' if self.rsa_encrypt_iv else 'key'}) + AES-{self.aes_mode}-{self.aes_padding} + RSA-{self.rsa_padding}"


def all_encoder_variants(pub: str) -> list[RPACEncoder]:
    """生成所有加密组合，用于暴力测试。"""
    variants = []
    for rsa_pad in ["pkcs1v15", "oaep"]:
        for rsa_iv in [True, False]:
            for aes_mode in ["cbc", "ecb"]:
                for aes_pad in ["zero", "pkcs7"]:
                    variants.append(RPACEncoder.from_pub_string(
                        pub,
                        rsa_encrypt_iv=rsa_iv,
                        aes_mode=aes_mode,
                        aes_padding=aes_pad,
                        rsa_padding=rsa_pad,
                    ))
    return variants
