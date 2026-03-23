"""SMTP email tool implementation for the agent."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from typing import Any, Dict, Optional


def _smtp_config() -> Dict[str, Any]:
    return {
        "host": os.getenv("SMTP_HOST", "smtp.gmail.com").strip(),
        "port": int(os.getenv("SMTP_PORT", "587") or "587"),
        "username": os.getenv("SMTP_USERNAME", "").strip(),
        "password": os.getenv("SMTP_PASSWORD", ""),
    }


def send_email(
    to: str,
    subject: str,
    body: str,
    from_email: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an email using configured SMTP settings."""
    config = _smtp_config()

    if not to or not subject or not body:
        return {"error": "Missing required fields: to, subject, and body are required."}

    if not config["host"] or not config["username"] or not config["password"]:
        return {
            "error": (
                "SMTP is not configured. Set SMTP_HOST, SMTP_USERNAME, and SMTP_PASSWORD "
                "in settings before sending email."
            )
        }

    sender = (from_email or config["username"]).strip()
    if not sender:
        return {"error": "A sender email could not be determined."}

    message = EmailMessage()
    message["From"] = sender
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    message.set_content("This email contains HTML content. Use an HTML-compatible email client to view it.")
    message.add_alternative(body, subtype="html")

    try:
        port = int(config["port"])
        if port == 465:
            with smtplib.SMTP_SSL(config["host"], port, timeout=30) as smtp:
                smtp.login(config["username"], config["password"])
                smtp.send_message(message)
        else:
            with smtplib.SMTP(config["host"], port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(config["username"], config["password"])
                smtp.send_message(message)
    except Exception as exc:
        return {"error": f"Failed to send email: {str(exc)}"}

    return {
        "message": f"Email sent successfully to {to.strip()}",
        "to": to.strip(),
        "subject": subject.strip(),
        "from": sender,
    }