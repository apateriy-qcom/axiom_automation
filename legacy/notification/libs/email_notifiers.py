"""Stub Email notifier — replaces the missing internal notification package."""
import logging
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)

class Email:
    def __init__(self, recipients, subject, body):
        self.recipients = recipients if isinstance(recipients, list) else [recipients]
        self.subject    = subject
        self.body       = body

    def send(self):
        try:
            msg = MIMEText(self.body)
            msg['Subject'] = self.subject
            msg['From']    = 'axiom-automation@localhost'
            msg['To']      = ', '.join(self.recipients)
            with smtplib.SMTP('localhost', 25, timeout=5) as s:
                s.sendmail(msg['From'], self.recipients, msg.as_string())
            logger.info('Email sent to %s', self.recipients)
        except Exception as exc:
            logger.warning('Email stub: could not send (%s) — continuing', exc)
