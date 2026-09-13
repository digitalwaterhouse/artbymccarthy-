"""Send the artist an email when somebody writes in.

Until now an enquiry only landed in the database, which means it only existed
if somebody thought to open the admin. Nobody checks an admin they have no
reason to open, so the enquiry form was effectively a hole in the floor.

Everything is read from the environment so this survives the move to the
artist's own host: point SMTP_HOST at whatever she has there and nothing else
changes. With no SMTP_HOST reachable, send() returns False and the caller
carries on -- the database is still the record, and a failed notification must
never cost a customer their message.

    python3 mailer.py you@example.com     # send yourself a test
"""
import os
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

HOST = os.environ.get("SMTP_HOST", "localhost")
PORT = int(os.environ.get("SMTP_PORT", "25"))
USER = os.environ.get("SMTP_USER", "")
PASS = os.environ.get("SMTP_PASS", "")
STARTTLS = os.environ.get("SMTP_STARTTLS", "0") == "1"
FROM = os.environ.get("MAIL_FROM", "Art by McCarthy <noreply@digitalwaterhouse.com>")
SENDMAIL = os.environ.get("SENDMAIL_PATH", "/usr/sbin/sendmail")
# Handing the message to the local sendmail binary is submission by a trusted
# local user; talking to postfix over SMTP on :25 is a relay attempt, and a
# mail server rightly refuses to relay for an address that is not one of its
# own mailboxes ("Relay access denied"). Prefer sendmail where it exists and
# fall back to SMTP, which is what a host without a local MTA will need.
TRANSPORT = os.environ.get("MAIL_TRANSPORT", "")

KIND = {"purchase": "Purchase enquiry", "commission": "Commission enquiry",
        "contact": "Message"}


# A staging copy of the site holds a copy of her contacts and a copy of her
# enquiries. If it can also send, a test press reaches real people from an
# address that looks like hers -- which is the classic way a staging
# environment does actual damage. So it cannot send, and not by having been
# left unconfigured: a hard refusal that survives somebody helpfully pasting
# the SMTP settings across one afternoon.
ENV_NAME = os.environ.get("ENV_NAME", "").strip()


def send(to, subject, body, reply_to=None):
    if ENV_NAME:
        print("[%s] refusing to send mail to %r -- subject %r"
              % (ENV_NAME, to, subject), file=sys.stderr)
        return False
    return _send(to, subject, body, reply_to)


def _send(to, subject, body, reply_to=None):
    """Returns True if the server accepted it. Never raises."""
    if not to:
        return False
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to and "@" in reply_to:
        # So she can just hit reply and be writing to the buyer, not to a
        # noreply box she does not own.
        name, addr = parseaddr(reply_to)
        msg["Reply-To"] = formataddr((name, addr)) if addr else reply_to
    msg.set_content(body)

    how = TRANSPORT or ("sendmail" if os.path.exists(SENDMAIL) else "smtp")
    try:
        if how == "sendmail":
            # -f sets the ENVELOPE sender. Without it the wrapper stamps its
            # own (root@p6.omnivalve.us here), and SPF is checked against the
            # envelope, not the From: header -- so the domain's SPF record
            # would never be the one consulted.
            cmd = [SENDMAIL, "-t", "-oi"]
            env_from = parseaddr(FROM)[1]
            if env_from:
                cmd += ["-f", env_from]
            p = subprocess.run(cmd, input=msg.as_bytes(),
                               capture_output=True, timeout=20)
            return p.returncode == 0
        with smtplib.SMTP(HOST, PORT, timeout=15) as s:
            if STARTTLS:
                s.starttls()
            if USER:
                s.login(USER, PASS)
            s.send_message(msg)
        return True
    except Exception:
        return False


def notify_inquiry(to, kind, name, email, body, work=None, admin_url=None):
    what = KIND.get(kind, "Enquiry")
    subject = "%s from %s" % (what, name or email or "someone")
    if work:
        subject += " — %s" % work
    lines = ["%s via the website." % what, ""]
    if work:
        lines += ["Work: %s" % work]
    lines += ["From: %s" % (name or "(no name given)"),
              "Email: %s" % (email or "(none given)"), "", "-" * 40, "",
              (body or "").strip() or "(no message)", "", "-" * 40, ""]
    if admin_url:
        lines += ["All enquiries: %s" % admin_url]
    lines += ["", "Reply to this email to answer them directly."]
    return send(to, subject, "\n".join(lines), reply_to=email)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python3 mailer.py you@example.com")
    ok = send(sys.argv[1], "Art by McCarthy — test",
              "If you are reading this, the enquiry notifications work.\n"
              "Sent via %s as %s\n" % (TRANSPORT or "auto", FROM))
    print("accepted by the server" if ok else "NOT SENT — check SMTP_HOST/SMTP_PORT")
