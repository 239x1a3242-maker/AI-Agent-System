#!/usr/bin/env python3
"""
send-email.py – Secure SMTP Email Sending Tool for AI Agents

Features:
    • TLS/SSL encryption
    • Rate limiting (token bucket)
    • Idempotency (duplicate prevention)
    • LLM output sanitization & validation
    • Exponential backoff with retries
    • MIME construction (text/html + attachments)
    • Comprehensive logging & error handling

Environment Variables (set before running):
    SMTP_HOST        – e.g. smtp.gmail.com
    SMTP_PORT        – 587 (STARTTLS) or 465 (SSL)
    SMTP_USER        – your email address
    SMTP_PASS        – app password (not your regular password)
    SMTP_FROM_EMAIL  – optional, defaults to SMTP_USER
    SMTP_USE_TLS     – true/false (default true)
    SMTP_TIMEOUT     – seconds (default 30)

    EMAIL_RATE_LIMIT        – max emails per minute (default 10)
    EMAIL_BURST_LIMIT       – burst capacity (default 3)
    EMAIL_MAX_RETRIES       – retry attempts (default 3)
    EMAIL_BASE_DELAY        – initial backoff seconds (default 1.0)
    EMAIL_MAX_DELAY         – max backoff seconds (default 60.0)

Usage (as a tool):
    from send_email import send_email

    result = send_email(
        to="user@example.com",
        subject="Hello",
        body="Plain text body",
        body_html="<h1>HTML version</h1>",
        attachments=["/path/to/file.pdf"],
        cc=["cc@example.com"],
        bcc=["bcc@example.com"]
    )
    # result is a dict with status, message_id, error, etc.
"""

import os
import re
import ssl
import json
import time
import hashlib
import logging
import smtplib
import random
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Dict, List, Optional, Union, Tuple, Any
from dataclasses import dataclass, field
from enum import Enum
from collections import deque
import threading

# ============================================================================
# CONFIGURATION (from environment)
# ============================================================================
from dotenv import load_dotenv
load_dotenv()  # loads variables from .env into environment
class SMTPConfig:
    GMAIL = {"host": "smtp.gmail.com", "port": 587, "use_tls": True, "use_ssl": False}
    OUTLOOK = {"host": "smtp.office365.com", "port": 587, "use_tls": True, "use_ssl": False}
    YAHOO = {"host": "smtp.mail.yahoo.com", "port": 465, "use_tls": False, "use_ssl": True}
    CUSTOM = {
        "host": os.getenv("SMTP_CUSTOM_HOST", ""),
        "port": int(os.getenv("SMTP_CUSTOM_PORT", "587")),
        "use_tls": os.getenv("SMTP_CUSTOM_TLS", "true").lower() == "true",
        "use_ssl": os.getenv("SMTP_CUSTOM_SSL", "false").lower() == "true"
    }

class RateLimitConfig:
    MAX_EMAILS_PER_MINUTE = int(os.getenv("EMAIL_RATE_LIMIT", "10"))
    BURST_CAPACITY = int(os.getenv("EMAIL_BURST_LIMIT", "3"))
    TOKEN_REFILL_RATE = 1.0 / (60.0 / MAX_EMAILS_PER_MINUTE)  # tokens per second

class RetryConfig:
    MAX_RETRIES = int(os.getenv("EMAIL_MAX_RETRIES", "3"))
    BASE_DELAY = float(os.getenv("EMAIL_BASE_DELAY", "1.0"))
    MAX_DELAY = float(os.getenv("EMAIL_MAX_DELAY", "60.0"))
    JITTER_RANGE = (0, 1)

class ValidationConfig:
    MAX_SUBJECT_LENGTH = 998
    MAX_BODY_LENGTH = 10 * 1024 * 1024   # 10 MB
    MAX_ATTACHMENT_SIZE = 25 * 1024 * 1024  # 25 MB
    ALLOWED_ATTACHMENT_TYPES = {
        '.pdf', '.doc', '.docx', '.txt', '.csv', '.xls', '.xlsx',
        '.png', '.jpg', '.jpeg', '.gif', '.zip', '.json', '.md'
    }

# ============================================================================
# DATA STRUCTURES
# ============================================================================

class SendStatus(Enum):
    SUCCESS = "success"
    VALIDATION_ERROR = "validation_error"
    RATE_LIMITED = "rate_limited"
    DUPLICATE = "duplicate"
    AUTH_ERROR = "authentication_error"
    NETWORK_ERROR = "network_error"
    SMTP_ERROR = "smtp_error"
    PERMANENT_FAILURE = "permanent_failure"

@dataclass
class EmailPayload:
    to: Union[str, List[str]]
    subject: str
    body_text: str
    body_html: Optional[str] = None
    attachments: List[str] = field(default_factory=list)
    cc: Optional[Union[str, List[str]]] = None
    bcc: Optional[Union[str, List[str]]] = None
    reply_to: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Normalize recipients to lists
        if isinstance(self.to, str):
            self.to = [self.to]
        if isinstance(self.cc, str):
            self.cc = [self.cc]
        if isinstance(self.bcc, str):
            self.bcc = [self.bcc]

@dataclass
class SendResult:
    status: SendStatus
    message_id: Optional[str] = None
    error: Optional[str] = None
    retry_count: int = 0
    timestamp: datetime = field(default_factory=datetime.now)
    request_hash: Optional[str] = None
    duration_ms: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "status": self.status.value,
            "message_id": self.message_id,
            "error": self.error,
            "retry_count": self.retry_count,
            "timestamp": self.timestamp.isoformat(),
            "request_hash": self.request_hash,
            "duration_ms": self.duration_ms
        }

# ============================================================================
# RATE LIMITER (Token Bucket)
# ============================================================================

class TokenBucketRateLimiter:
    def __init__(self, capacity: int = RateLimitConfig.BURST_CAPACITY,
                 refill_rate: float = RateLimitConfig.TOKEN_REFILL_RATE):
        self.capacity = capacity
        self.tokens = float(capacity)
        self.refill_rate = refill_rate
        self.last_refill = time.time()
        self._lock = threading.Lock()

    def _refill(self):
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def acquire(self, tokens: int = 1, timeout: Optional[float] = None) -> bool:
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            if timeout is None:
                return False
            tokens_needed = tokens - self.tokens
            wait_time = tokens_needed / self.refill_rate
            if wait_time > timeout:
                return False
        # Wait outside lock
        if timeout and wait_time <= timeout:
            time.sleep(wait_time)
            return self.acquire(tokens, timeout=0)
        return False

    def get_status(self) -> Dict:
        with self._lock:
            self._refill()
            return {
                "available_tokens": round(self.tokens, 2),
                "capacity": self.capacity,
                "refill_rate_per_sec": self.refill_rate
            }

# ============================================================================
# IDEMPOTENCY CACHE
# ============================================================================

class IdempotencyCache:
    def __init__(self, ttl_seconds: int = 3600):
        self.cache: Dict[str, datetime] = {}
        self.ttl = timedelta(seconds=ttl_seconds)
        self._lock = threading.Lock()

    def _clean_expired(self):
        now = datetime.now()
        expired = [k for k, v in self.cache.items() if now - v > self.ttl]
        for k in expired:
            del self.cache[k]

    def check_and_set(self, request_hash: str) -> bool:
        with self._lock:
            self._clean_expired()
            if request_hash in self.cache:
                return False
            self.cache[request_hash] = datetime.now()
            return True

    def get_stats(self) -> Dict:
        with self._lock:
            self._clean_expired()
            return {"active_entries": len(self.cache), "ttl_seconds": self.ttl.total_seconds()}

# ============================================================================
# INPUT VALIDATOR & SANITIZER
# ============================================================================

class InputValidator:
    EMAIL_REGEX = re.compile(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$')
    SPAM_PATTERNS = [
        r'\b(viagra|cialis|casino|lottery|winner|prize)\b',
        r'!!!+', r'\$\$\$+', r'URGENT', r'ACT NOW', r'click here', r'limited time',
    ]

    @classmethod
    def validate_email(cls, email: str) -> Tuple[bool, str]:
        if not email or not isinstance(email, str):
            return False, "Email is empty or invalid type"
        email = email.strip().lower()
        if len(email) > 254:
            return False, "Email exceeds maximum length"
        if not cls.EMAIL_REGEX.match(email):
            return False, f"Invalid email format: {email}"
        return True, ""

    @classmethod
    def sanitize_text(cls, text: str, max_length: int, field_name: str) -> Tuple[str, Optional[str]]:
        if not isinstance(text, str):
            return "", f"{field_name} must be string"
        text = ' '.join(text.split())  # normalize whitespace
        if len(text) > max_length:
            return "", f"{field_name} exceeds maximum length of {max_length}"
        if not text.strip():
            return "", f"{field_name} cannot be empty"
        # Basic XSS protection (remove script tags)
        text = text.replace('<script>', '').replace('</script>', '')
        return text, None

    @classmethod
    def check_spam_indicators(cls, subject: str, body: str) -> Tuple[bool, List[str]]:
        violations = []
        combined = f"{subject} {body}".lower()
        for pattern in cls.SPAM_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                violations.append(pattern)
        caps_count = sum(1 for c in combined if c.isupper())
        if len(combined) > 0 and caps_count / len(combined) > 0.5:
            violations.append("EXCESSIVE_CAPITALIZATION")
        return len(violations) == 0, violations

    @classmethod
    def validate_attachment(cls, filepath: str) -> Tuple[bool, str]:
        path = Path(filepath)
        if not path.exists():
            return False, f"File not found: {filepath}"
        if not path.is_file():
            return False, f"Path is not a file: {filepath}"
        ext = path.suffix.lower()
        if ext not in ValidationConfig.ALLOWED_ATTACHMENT_TYPES:
            return False, f"File type not allowed: {ext}"
        size = path.stat().st_size
        if size > ValidationConfig.MAX_ATTACHMENT_SIZE:
            return False, f"File too large: {size} bytes (max {ValidationConfig.MAX_ATTACHMENT_SIZE})"
        return True, ""

    @classmethod
    def validate_payload(cls, payload: EmailPayload) -> Tuple[bool, List[str]]:
        errors = []

        # Recipients
        for email in payload.to:
            valid, err = cls.validate_email(email)
            if not valid:
                errors.append(f"To: {err}")
        if payload.cc:
            for email in payload.cc:
                valid, err = cls.validate_email(email)
                if not valid:
                    errors.append(f"CC: {err}")
        if payload.bcc:
            for email in payload.bcc:
                valid, err = cls.validate_email(email)
                if not valid:
                    errors.append(f"BCC: {err}")
        if payload.reply_to:
            valid, err = cls.validate_email(payload.reply_to)
            if not valid:
                errors.append(f"Reply-To: {err}")

        # Subject & body
        subject, err = cls.sanitize_text(payload.subject, ValidationConfig.MAX_SUBJECT_LENGTH, "Subject")
        if err:
            errors.append(err)
        else:
            payload.subject = subject

        body_text, err = cls.sanitize_text(payload.body_text, ValidationConfig.MAX_BODY_LENGTH, "Body")
        if err:
            errors.append(err)
        else:
            payload.body_text = body_text

        # Spam check
        is_clean, violations = cls.check_spam_indicators(payload.subject, payload.body_text)
        if not is_clean:
            errors.append(f"Spam indicators detected: {violations}")

        # Attachments
        for filepath in payload.attachments:
            valid, err = cls.validate_attachment(filepath)
            if not valid:
                errors.append(f"Attachment: {err}")

        return len(errors) == 0, errors

# ============================================================================
# EMAIL BUILDER (MIME)
# ============================================================================

class EmailBuilder:
    @staticmethod
    def build_message(payload: EmailPayload) -> MIMEMultipart:
        msg = MIMEMultipart('mixed')

        # Headers
        msg['From'] = os.getenv("SMTP_FROM_EMAIL", os.getenv("SMTP_USER"))
        msg['To'] = ', '.join(payload.to)
        msg['Subject'] = payload.subject
        msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S %z')
        msg['Message-ID'] = f"<{hashlib.sha256(str(time.time()).encode()).hexdigest()[:16]}@{os.getenv('SMTP_HOST', 'localhost')}>"

        if payload.cc:
            msg['Cc'] = ', '.join(payload.cc)
        if payload.reply_to:
            msg['Reply-To'] = payload.reply_to

        # Alternative part (text + html)
        alt_part = MIMEMultipart('alternative')
        alt_part.attach(MIMEText(payload.body_text, 'plain', 'utf-8'))
        if payload.body_html:
            alt_part.attach(MIMEText(payload.body_html, 'html', 'utf-8'))
        msg.attach(alt_part)

        # Attachments
        for filepath in payload.attachments:
            EmailBuilder._attach_file(msg, filepath)

        return msg

    @staticmethod
    def _attach_file(msg: MIMEMultipart, filepath: str):
        path = Path(filepath)
        mime_type = EmailBuilder._get_mime_type(path.suffix)
        main_type, sub_type = mime_type.split('/', 1)

        with open(filepath, 'rb') as f:
            attachment = MIMEBase(main_type, sub_type)
            attachment.set_payload(f.read())

        encoders.encode_base64(attachment)
        attachment.add_header('Content-Disposition', f'attachment; filename="{path.name}"')
        msg.attach(attachment)

    @staticmethod
    def _get_mime_type(ext: str) -> str:
        mime_map = {
            '.pdf': 'application/pdf',
            '.doc': 'application/msword',
            '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            '.txt': 'text/plain',
            '.csv': 'text/csv',
            '.xls': 'application/vnd.ms-excel',
            '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.zip': 'application/zip',
            '.json': 'application/json',
            '.md': 'text/markdown'
        }
        return mime_map.get(ext.lower(), 'application/octet-stream')

# ============================================================================
# SMTP CLIENT WITH RETRY LOGIC
# ============================================================================

class SMTPClient:
    RETRYABLE_CODES = {420, 421, 450, 451, 452, 454}  # RFC transient errors

    def __init__(self):
        self.host = os.getenv("SMTP_HOST", "smtp.gmail.com")
        self.port = int(os.getenv("SMTP_PORT", "587"))
        self.user = os.getenv("SMTP_USER")
        self.password = os.getenv("SMTP_PASS") or os.getenv("SMTP_PASSWORD")
        self.use_tls = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
        self.timeout = int(os.getenv("SMTP_TIMEOUT", "30"))

        if not all([self.user, self.password]):
            raise ValueError("SMTP_USER and SMTP_PASS environment variables required")

    def _calculate_backoff(self, attempt: int) -> float:
        delay = min(RetryConfig.BASE_DELAY * (2 ** attempt), RetryConfig.MAX_DELAY)
        jitter = random.uniform(*RetryConfig.JITTER_RANGE)
        return delay + jitter

    def _is_retryable_error(self, exception: Exception) -> bool:
        if isinstance(exception, smtplib.SMTPAuthenticationError):
            return False
        if isinstance(exception, (ConnectionError, TimeoutError, OSError)):
            return True
        if isinstance(exception, smtplib.SMTPResponseException):
            return exception.smtp_code in self.RETRYABLE_CODES
        if isinstance(exception, smtplib.SMTPServerDisconnected):
            return True
        return False

    def send_with_retry(self, msg: MIMEMultipart, recipients: List[str]) -> Tuple[bool, Optional[str], int]:
        last_exception = None
        for attempt in range(RetryConfig.MAX_RETRIES + 1):
            try:
                success, result = self._send_once(msg, recipients)
                if success:
                    return True, result, attempt
                if isinstance(result, smtplib.SMTPResponseException):
                    if not self._is_retryable_error(result):
                        return False, str(result), attempt
                    last_exception = result
                else:
                    return False, result, attempt
            except Exception as e:
                last_exception = e
                if not self._is_retryable_error(e):
                    return False, str(e), attempt

            if attempt < RetryConfig.MAX_RETRIES:
                backoff = self._calculate_backoff(attempt)
                logging.warning(f"Retry {attempt+1}/{RetryConfig.MAX_RETRIES} after {backoff:.1f}s: {last_exception}")
                time.sleep(backoff)

        return False, f"Max retries exceeded. Last error: {last_exception}", RetryConfig.MAX_RETRIES

    def _send_once(self, msg: MIMEMultipart, recipients: List[str]) -> Tuple[bool, Union[str, Exception]]:
        server = None
        try:
            server = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.use_tls:
                context = ssl.create_default_context()
                server.starttls(context=context)
                server.ehlo()
            server.login(self.user, self.password)
            result = server.sendmail(from_addr=msg['From'], to_addrs=recipients, msg=msg.as_string())
            if result:
                return False, f"Some recipients rejected: {result}"
            return True, msg['Message-ID']
        except smtplib.SMTPAuthenticationError as e:
            logging.error(f"Authentication failed: {e}")
            return False, e
        except smtplib.SMTPException as e:
            logging.error(f"SMTP error: {e}")
            return False, e
        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            return False, e
        finally:
            if server:
                try:
                    server.quit()
                except:
                    pass

# ============================================================================
# MAIN EMAIL SERVICE
# ============================================================================

class EmailService:
    def __init__(self):
        self.rate_limiter = TokenBucketRateLimiter()
        self.idempotency_cache = IdempotencyCache()
        self.smtp_client = SMTPClient()
        self.validator = InputValidator()
        self.builder = EmailBuilder()
        logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        self.logger = logging.getLogger(__name__)

    def _generate_request_hash(self, payload: EmailPayload) -> str:
        content = f"{''.join(payload.to)}:{payload.subject}:{payload.body_text[:100]}"
        return hashlib.sha256(content.encode()).hexdigest()[:32]

    def send(self, payload: EmailPayload) -> SendResult:
        start_time = time.time()
        request_hash = self._generate_request_hash(payload)
        self.logger.info(f"Email request received: hash={request_hash}, to={payload.to}")

        # 1. Idempotency
        if not self.idempotency_cache.check_and_set(request_hash):
            self.logger.warning(f"Duplicate request dropped: {request_hash}")
            return SendResult(
                status=SendStatus.DUPLICATE,
                request_hash=request_hash,
                error="Duplicate request - already processed",
                duration_ms=(time.time() - start_time) * 1000
            )

        # 2. Validation
        is_valid, errors = self.validator.validate_payload(payload)
        if not is_valid:
            error_msg = "; ".join(errors)
            self.logger.error(f"Validation failed: {error_msg}")
            return SendResult(
                status=SendStatus.VALIDATION_ERROR,
                request_hash=request_hash,
                error=error_msg,
                duration_ms=(time.time() - start_time) * 1000
            )

        # 3. Rate limiting
        if not self.rate_limiter.acquire(timeout=60):
            self.logger.warning(f"Rate limit exceeded: {request_hash}")
            return SendResult(
                status=SendStatus.RATE_LIMITED,
                request_hash=request_hash,
                error="Rate limit exceeded - too many emails sent recently",
                duration_ms=(time.time() - start_time) * 1000
            )

        # 4. Build MIME message
        try:
            msg = self.builder.build_message(payload)
        except Exception as e:
            self.logger.error(f"Message building failed: {e}")
            return SendResult(
                status=SendStatus.PERMANENT_FAILURE,
                request_hash=request_hash,
                error=f"Failed to build email: {str(e)}",
                duration_ms=(time.time() - start_time) * 1000
            )

        # 5. Combine recipients
        all_recipients = payload.to.copy()
        if payload.cc:
            all_recipients.extend(payload.cc)
        if payload.bcc:
            all_recipients.extend(payload.bcc)

        # 6. Send with retry
        try:
            success, result, retry_count = self.smtp_client.send_with_retry(msg, all_recipients)
            duration_ms = (time.time() - start_time) * 1000

            if success:
                self.logger.info(f"Email sent: message_id={result}, retries={retry_count}")
                return SendResult(
                    status=SendStatus.SUCCESS,
                    message_id=result,
                    retry_count=retry_count,
                    request_hash=request_hash,
                    duration_ms=duration_ms
                )
            else:
                if "authentication" in str(result).lower():
                    status = SendStatus.AUTH_ERROR
                elif "network" in str(result).lower() or "timeout" in str(result).lower():
                    status = SendStatus.NETWORK_ERROR
                else:
                    status = SendStatus.SMTP_ERROR
                self.logger.error(f"Email failed after {retry_count} retries: {result}")
                return SendResult(
                    status=status,
                    error=str(result),
                    retry_count=retry_count,
                    request_hash=request_hash,
                    duration_ms=duration_ms
                )
        except Exception as e:
            self.logger.exception("Unexpected error in send flow")
            return SendResult(
                status=SendStatus.PERMANENT_FAILURE,
                error=f"Unexpected error: {str(e)}",
                request_hash=request_hash,
                duration_ms=(time.time() - start_time) * 1000
            )

    def get_status(self) -> Dict:
        return {
            "rate_limiter": self.rate_limiter.get_status(),
            "idempotency_cache": self.idempotency_cache.get_stats(),
            "smtp_configured": bool(os.getenv("SMTP_HOST")),
            "timestamp": datetime.now().isoformat()
        }

# ============================================================================
# CONVENIENCE FUNCTIONS (Singleton)
# ============================================================================

_email_service: Optional[EmailService] = None
_service_lock = threading.Lock()

def get_email_service() -> EmailService:
    global _email_service
    if _email_service is None:
        with _service_lock:
            if _email_service is None:
                _email_service = EmailService()
    return _email_service

def send_email(
    to: Union[str, List[str]],
    subject: str,
    body: str,
    body_html: Optional[str] = None,
    attachments: Optional[List[str]] = None,
    cc: Optional[Union[str, List[str]]] = None,
    bcc: Optional[Union[str, List[str]]] = None
) -> Dict:
    """
    Send an email with full safety controls.

    Args:
        to: Recipient email address(es).
        subject: Email subject.
        body: Plain text body.
        body_html: Optional HTML version.
        attachments: List of file paths to attach.
        cc: Carbon copy recipient(s).
        bcc: Blind carbon copy recipient(s).

    Returns:
        Dictionary with keys:
            - status: one of "success", "validation_error", "rate_limited", "duplicate",
                      "authentication_error", "network_error", "smtp_error", "permanent_failure"
            - message_id: if successful
            - error: error description if failed
            - retry_count: number of retry attempts
            - timestamp: ISO timestamp
            - request_hash: unique hash for this request
            - duration_ms: processing time in milliseconds
    """
    payload = EmailPayload(
        to=to,
        subject=subject,
        body_text=body,
        body_html=body_html,
        attachments=attachments or [],
        cc=cc,
        bcc=bcc
    )
    service = get_email_service()
    result = service.send(payload)
    return result.to_dict()

# ============================================================================
# EXAMPLE USAGE & TESTING (when run directly)
# ============================================================================

if __name__ == "__main__":
    print("Email Service Test Suite")
    print("=" * 50)

    # Check required environment variables
    required = ["SMTP_HOST", "SMTP_USER", "SMTP_PASS"]
    missing = [v for v in required if not os.getenv(v)]
    if missing:
        print(f"❌ Missing environment variables: {missing}")
        print("Please set them before running tests.")
        exit(1)

    service = get_email_service()
    print("✅ Email service initialized")
    print(f"Status: {json.dumps(service.get_status(), indent=2)}")

    # Test 1: Validation failure
    print("\n--- Test 1: Invalid Email ---")
    result = send_email(
        to="invalid-email",
        subject="Test",
        body="This should fail validation"
    )
    print(f"Result: {json.dumps(result, indent=2)}")

    # Test 2: Rate limiter status
    print("\n--- Test 2: Rate Limiter ---")
    print(f"Rate limiter: {service.rate_limiter.get_status()}")

    # Test 3: Idempotency
    print("\n--- Test 3: Idempotency ---")
    payload = EmailPayload(
        to=["test@example.com"],
        subject="Duplicate Test",
        body_text="Testing duplicate detection"
    )
    h1 = service._generate_request_hash(payload)
    h2 = service._generate_request_hash(payload)
    print(f"Same payload generates same hash: {h1 == h2} ({h1})")

    # Test 4: Real send (commented out by default)
    """
    print("\n--- Test 4: Live Send ---")
    result = send_email(
        to=["your_test_email@example.com"],
        subject="Test from AI Agent",
        body="This is a test email sent by the AI agent email service.",
        body_html="<h1>Test Email</h1><p>This is <b>HTML</b> content.</p>"
    )
    print(f"Send result: {json.dumps(result, indent=2)}")
    """

    print("\n✅ All tests completed")