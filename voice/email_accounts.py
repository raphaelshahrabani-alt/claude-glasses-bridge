"""All of the user's email accounts, over IMAP and SMTP.

Accounts live in ~/.claude-glasses-bridge/email-accounts.json; their app
passwords live in the macOS Keychain (through `keyring`), or, where there is
no keychain, in email-secrets.json next to it, readable only by the user.

    glasses-email add you@example.com     add an account (asks for its app password)
    glasses-email list                    the accounts, and whether each one logs in
    glasses-email remove you@example.com

email_mcp.py serves these accounts to Claude Code as tools.
"""
from __future__ import annotations

import concurrent.futures
import email
import email.policy
import email.utils
import getpass
import imaplib
import json
import os
import re
import shutil
import smtplib
import ssl
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

DATA_DIR = Path(os.environ.get("GLASSES_DATA_DIR", Path.home() / ".claude-glasses-bridge")).expanduser()
KEYRING_SERVICE = "claude-glasses-email"
TIMEOUT_S = 20

# Mail servers for the common providers, by the domain of the address or of its MX records.
PROVIDERS: dict[str, dict[str, Any]] = {
    "gmail": {"imap": ("imap.gmail.com", 993), "smtp": ("smtp.gmail.com", 465), "saves_sent": True,
              "help": "Google account → Security → 2-Step Verification on → App passwords"},
    "icloud": {"imap": ("imap.mail.me.com", 993), "smtp": ("smtp.mail.me.com", 587), "saves_sent": False,
               "help": "account.apple.com → Sign-In and Security → App-Specific Passwords"},
    "yahoo": {"imap": ("imap.mail.yahoo.com", 993), "smtp": ("smtp.mail.yahoo.com", 465), "saves_sent": True,
              "help": "Yahoo account security → Generate app password"},
    "fastmail": {"imap": ("imap.fastmail.com", 993), "smtp": ("smtp.fastmail.com", 465), "saves_sent": False,
                 "help": "Fastmail settings → Privacy & Security → App passwords"},
    "zoho": {"imap": ("imap.zoho.com", 993), "smtp": ("smtp.zoho.com", 465), "saves_sent": False,
             "help": "Zoho account → Security → App passwords"},
}
DOMAINS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
    "yahoo.com": "yahoo", "yahoo.ca": "yahoo", "ymail.com": "yahoo",
    "fastmail.com": "fastmail", "zoho.com": "zoho",
}
MX_HINTS = {"google.com": "gmail", "googlemail.com": "gmail", "icloud.com": "icloud", "zoho.com": "zoho",
            "messagingengine.com": "fastmail", "yahoodns.net": "yahoo"}
MICROSOFT = ("outlook.com", "hotmail.com", "live.com", "msn.com", "outlook.ca", "hotmail.ca")


@dataclass
class Account:
    address: str
    imap_host: str
    imap_port: int
    smtp_host: str
    smtp_port: int
    provider: str = "other"
    username: str = ""
    name: str = ""  # the name recipients see

    @property
    def login(self) -> str:
        return self.username or self.address

    @property
    def gmail(self) -> bool:
        return self.provider == "gmail"


@dataclass
class Store:
    """Where accounts and their passwords are kept."""
    directory: Path = field(default_factory=lambda: DATA_DIR)

    @property
    def accounts_file(self) -> Path:
        return self.directory / "email-accounts.json"

    @property
    def secrets_file(self) -> Path:
        return self.directory / "email-secrets.json"

    def accounts(self) -> list[Account]:
        try:
            raw = json.loads(self.accounts_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [Account(**a) for a in raw]

    def save(self, accounts: list[Account]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self.accounts_file.write_text(json.dumps([asdict(a) for a in accounts], indent=2), encoding="utf-8")

    def _keyring(self):
        if os.environ.get("GLASSES_EMAIL_STORE") == "file":
            return None
        try:
            import keyring

            if "fail" in type(keyring.get_keyring()).__name__.lower():
                return None
            return keyring
        except Exception:
            return None

    def password(self, address: str) -> str | None:
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(KEYRING_SERVICE, address)
                if value:
                    return value
            except Exception:
                pass
        try:
            return json.loads(self.secrets_file.read_text(encoding="utf-8")).get(address)
        except (OSError, json.JSONDecodeError):
            return None

    def set_password(self, address: str, password: str) -> str:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(KEYRING_SERVICE, address, password)
                return "the macOS Keychain"
            except Exception:
                pass
        secrets: dict[str, str] = {}
        try:
            secrets = json.loads(self.secrets_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
        secrets[address] = password
        self.directory.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.secrets_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(secrets, f)
        return str(self.secrets_file)

    def forget(self, address: str) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(KEYRING_SERVICE, address)
            except Exception:
                pass
        try:
            secrets = json.loads(self.secrets_file.read_text(encoding="utf-8"))
            if secrets.pop(address, None) is not None:
                self.secrets_file.write_text(json.dumps(secrets), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass


def mx_provider(domain: str) -> str | None:
    dig = shutil.which("dig")
    if not dig:
        return None
    try:
        out = subprocess.run([dig, "+short", "MX", domain], capture_output=True, text=True, timeout=5).stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return None
    if "outlook.com" in out:
        return "microsoft"
    for hint, provider in MX_HINTS.items():
        if hint in out:
            return provider
    return None


def detect(address: str) -> str | None:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in MICROSOFT:
        return "microsoft"
    return DOMAINS.get(domain) or mx_provider(domain)


# ---- reading and sending --------------------------------------------------

ImapFactory = Callable[[Account, str], Any]
SmtpFactory = Callable[[Account, str], Any]


def real_imap(account: Account, password: str) -> imaplib.IMAP4:
    conn = imaplib.IMAP4_SSL(account.imap_host, account.imap_port, timeout=TIMEOUT_S,
                             ssl_context=ssl.create_default_context())
    conn.login(account.login, password)
    return conn


def real_smtp(account: Account, password: str) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if account.smtp_port == 465:
        conn: smtplib.SMTP = smtplib.SMTP_SSL(account.smtp_host, account.smtp_port, timeout=TIMEOUT_S, context=context)
    else:
        conn = smtplib.SMTP(account.smtp_host, account.smtp_port, timeout=TIMEOUT_S)
        conn.starttls(context=context)
    conn.login(account.login, password)
    return conn


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _plain(msg: email.message.EmailMessage) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except (LookupError, ValueError):
        return ""
    if part.get_content_type() == "text/html":
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
    return re.sub(r"[ \t\r\f\v]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


def _special_folder(conn: Any, flag: str, fallback: str) -> str:
    """The folder marked with a special-use flag (\\Drafts, \\Sent), e.g. "[Gmail]/Drafts"."""
    try:
        typ, lines = conn.list()
    except Exception:
        return fallback
    for line in lines or []:
        text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
        if flag.lower() in text.lower():
            match = re.search(r'"([^"]+)"\s*$', text) or re.search(r"\s(\S+)\s*$", text)
            if match:
                return match.group(1)
    return fallback


class Mail:
    def __init__(self, store: Store | None = None, imap: ImapFactory = real_imap, smtp: SmtpFactory = real_smtp):
        self.store = store or Store()
        self.imap = imap
        self.smtp = smtp

    def accounts(self) -> list[Account]:
        return self.store.accounts()

    def account(self, which: str) -> Account:
        accounts = self.accounts()
        which = (which or "").strip().lower()
        for a in accounts:
            if a.address.lower() == which:
                return a
        matches = [a for a in accounts if which and which in a.address.lower()]
        if len(matches) == 1:
            return matches[0]
        known = ", ".join(a.address for a in accounts) or "none yet"
        raise ValueError(f"no single account matches {which!r}; the accounts are: {known}")

    def _connect(self, account: Account) -> Any:
        password = self.store.password(account.address)
        if not password:
            raise ValueError(f"no password saved for {account.address}; run: glasses-email add {account.address}")
        return self.imap(account, password)

    def search(self, account: str = "all", query: str = "", unread_only: bool = False,
               days: int = 7, limit: int = 10, folder: str = "INBOX") -> dict[str, Any]:
        targets = self.accounts() if account in ("", "all", None) else [self.account(account)]
        if not targets:
            return {"error": "no email accounts are set up; run: glasses-email add you@example.com"}
        results: dict[str, Any] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:
            futures = {pool.submit(self._search_one, a, query, unread_only, days, limit, folder): a for a in targets}
            for future in concurrent.futures.as_completed(futures):
                a = futures[future]
                try:
                    results[a.address] = future.result()
                except Exception as e:  # one broken account shouldn't hide the others
                    results[a.address] = {"error": f"{type(e).__name__}: {e}"}
        return results

    def _search_one(self, account: Account, query: str, unread_only: bool, days: int,
                    limit: int, folder: str) -> list[dict[str, Any]]:
        conn = self._connect(account)
        try:
            conn.select(_quote(folder), readonly=True)
            since = (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%d-%b-%Y")
            if account.gmail:
                raw = f"newer_than:{max(1, int(days))}d" + (" is:unread" if unread_only else "") + (f" {query}" if query else "")
                typ, data = conn.uid("SEARCH", "X-GM-RAW", _quote(raw))
            else:
                criteria = ["SINCE", since] + (["UNSEEN"] if unread_only else []) + (["TEXT", _quote(query)] if query else [])
                typ, data = conn.uid("SEARCH", *criteria)
            uids = (data[0] or b"").split() if data else []
            uids = uids[-max(1, int(limit)):][::-1]
            found = []
            for uid in uids:
                typ, parts = conn.uid("FETCH", uid, "(FLAGS RFC822.SIZE BODY.PEEK[HEADER])")
                header = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                # Servers put FLAGS before or after the header literal.
                meta = b" ".join(p[0] if isinstance(p, tuple) else p for p in parts if isinstance(p, (tuple, bytes)))
                msg = email.message_from_bytes(header, policy=email.policy.default)
                size = int(m.group(1)) if (m := re.search(rb"RFC822\.SIZE (\d+)", meta)) else 0
                snippet = ""
                if size and size < 300_000:
                    typ, body = conn.uid("FETCH", uid, "(BODY.PEEK[])")
                    raw_body = next((p[1] for p in body if isinstance(p, tuple)), b"")
                    snippet = _plain(email.message_from_bytes(raw_body, policy=email.policy.default))[:280]
                found.append({
                    "id": f"{account.address}|{folder}|{uid.decode()}",
                    "from": str(msg.get("from", "")),
                    "to": str(msg.get("to", "")),
                    "subject": str(msg.get("subject", "")),
                    "date": str(msg.get("date", "")),
                    "unread": b"\\Seen" not in meta,
                    "snippet": snippet,
                })
            return found
        finally:
            _logout(conn)

    def read(self, id: str) -> dict[str, Any]:
        address, folder, uid = id.split("|", 2)
        account = self.account(address)
        conn = self._connect(account)
        try:
            conn.select(_quote(folder), readonly=True)
            typ, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next((p[1] for p in parts if isinstance(p, tuple)), None)
            if raw is None:
                raise ValueError(f"no message {id}")
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            attachments = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()]
            return {
                "id": id,
                "account": account.address,
                "from": str(msg.get("from", "")),
                "to": str(msg.get("to", "")),
                "cc": str(msg.get("cc", "")),
                "subject": str(msg.get("subject", "")),
                "date": str(msg.get("date", "")),
                "message_id": str(msg.get("message-id", "")),
                "body": _plain(msg)[:12_000],
                "attachments": attachments,
            }
        finally:
            _logout(conn)

    def mark_read(self, id: str) -> str:
        address, folder, uid = id.split("|", 2)
        conn = self._connect(self.account(address))
        try:
            conn.select(_quote(folder))
            conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
            return "marked as read"
        finally:
            _logout(conn)

    def compose(self, from_account: str, to: str, subject: str, body: str, cc: str = "",
                bcc: str = "", reply_to_id: str = "") -> tuple[Account, EmailMessage]:
        account = self.account(from_account)
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((account.name, account.address)) if account.name else account.address
        msg["To"] = to
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain=account.address.rsplit("@", 1)[-1])
        if reply_to_id:
            original = self.read(reply_to_id)
            if original.get("message_id"):
                msg["In-Reply-To"] = original["message_id"]
                msg["References"] = original["message_id"]
            if not subject:
                subject = original["subject"]
            if subject and not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
        msg["Subject"] = subject
        msg.set_content(body)
        return account, msg

    def send(self, from_account: str, to: str, subject: str, body: str, cc: str = "",
             bcc: str = "", reply_to_id: str = "") -> str:
        account, msg = self.compose(from_account, to, subject, body, cc, bcc, reply_to_id)
        password = self.store.password(account.address)
        if not password:
            raise ValueError(f"no password saved for {account.address}; run: glasses-email add {account.address}")
        conn = self.smtp(account, password)
        try:
            conn.send_message(msg)
        finally:
            try:
                conn.quit()
            except Exception:
                pass
        if not PROVIDERS.get(account.provider, {}).get("saves_sent", False):
            # Most servers don't keep a copy of mail sent over SMTP; put one in Sent.
            try:
                imap = self._connect(account)
                try:
                    sent = _special_folder(imap, "\\Sent", "Sent")
                    imap.append(_quote(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
                finally:
                    _logout(imap)
            except Exception:
                pass
        return f"sent from {account.address} to {to}"

    def draft(self, from_account: str, to: str, subject: str, body: str, cc: str = "",
              reply_to_id: str = "") -> str:
        account, msg = self.compose(from_account, to, subject, body, cc, "", reply_to_id)
        conn = self._connect(account)
        try:
            drafts = _special_folder(conn, "\\Drafts", "[Gmail]/Drafts" if account.gmail else "Drafts")
            conn.append(_quote(drafts), "(\\Draft)", imaplib.Time2Internaldate(time.time()), msg.as_bytes())
            return f"saved a draft in {account.address} ({drafts})"
        finally:
            _logout(conn)

    def check(self, account: Account) -> str | None:
        """None when the account logs in to both IMAP and SMTP, else what went wrong."""
        password = self.store.password(account.address)
        if not password:
            return "no password saved"
        try:
            _logout(self.imap(account, password))
        except Exception as e:
            return f"reading mail failed: {e}"
        try:
            self.smtp(account, password).quit()
        except Exception as e:
            return f"sending mail failed: {e}"
        return None


def _logout(conn: Any) -> None:
    try:
        conn.logout()
    except Exception:
        pass


# ---- glasses-email ---------------------------------------------------------

def _host(value: str, default_port: int) -> tuple[str, int]:
    host, _, port = value.partition(":")
    return host, int(port or default_port)


def cli(argv: list[str], store: Store | None = None, mail: Mail | None = None,
        ask: Callable[[str], str] = lambda prompt: getpass.getpass(prompt)) -> int:
    store = store or Store()
    mail = mail or Mail(store)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.split("\n\n")[2] if __doc__ else "")
        print("Options for add: --provider gmail|icloud|yahoo|fastmail|zoho  --imap host[:port]  --smtp host[:port]")
        print("                 --username login  --name 'Name recipients see'")
        return 0
    command, rest = argv[0], argv[1:]
    accounts = store.accounts()

    if command == "list":
        if not accounts:
            print("No email accounts yet. Add one with: glasses-email add you@example.com")
            return 0
        for a in accounts:
            problem = mail.check(a)
            print(f"  {'✓' if problem is None else '✗'} {a.address} ({a.provider})" + (f": {problem}" if problem else ""))
        return 0

    if command == "remove":
        if not rest:
            print("usage: glasses-email remove you@example.com")
            return 1
        keep = [a for a in accounts if a.address.lower() != rest[0].lower()]
        store.save(keep)
        store.forget(rest[0])
        print(f"Removed {rest[0]}." if len(keep) < len(accounts) else f"{rest[0]} wasn't set up.")
        return 0

    if command != "add" or not rest:
        print("usage: glasses-email add you@example.com | list | remove you@example.com")
        return 1

    address = rest[0].strip()
    options: dict[str, str] = {}
    i = 1
    while i < len(rest):
        if rest[i].startswith("--") and i + 1 < len(rest):
            options[rest[i][2:]] = rest[i + 1]
            i += 2
        else:
            i += 1
    provider = options.get("provider") or detect(address) or "other"
    if provider == "microsoft" and not ("imap" in options and "smtp" in options):
        print(f"{address} is a Microsoft (Outlook) account. Microsoft no longer lets apps sign in with a")
        print("password, so it can't be added here. Connect it in Claude Code instead (the Microsoft 365")
        print("connector), or forward its mail to one of your other accounts.")
        return 1
    known = PROVIDERS.get(provider)
    if known:
        imap_host, imap_port = _host(options["imap"], 993) if "imap" in options else known["imap"]
        smtp_host, smtp_port = _host(options["smtp"], 465) if "smtp" in options else known["smtp"]
    elif "imap" in options and "smtp" in options:
        imap_host, imap_port = _host(options["imap"], 993)
        smtp_host, smtp_port = _host(options["smtp"], 465)
    else:
        print(f"I don't know the mail servers for {address}. Ask your email provider for its IMAP and SMTP")
        print(f"servers, then run: glasses-email add {address} --imap imap.example.com --smtp smtp.example.com")
        return 1
    account = Account(address=address, imap_host=imap_host, imap_port=imap_port, smtp_host=smtp_host,
                      smtp_port=smtp_port, provider=provider, username=options.get("username", ""),
                      name=options.get("name", ""))
    if known:
        print(f"{address}: {provider} account. Use an app password, not your normal password.")
        print(f"  Make one at: {known['help']}")
    password = ask(f"App password for {address}: ").replace(" ", "")
    if not password:
        print("No password given; nothing changed.")
        return 1
    previous = store.password(address)
    where = store.set_password(address, password)
    problem = mail.check(account)
    if problem:
        if previous:
            store.set_password(address, previous)
        else:
            store.forget(address)
        print(f"✗ {address} didn't log in: {problem}")
        print("  Check the app password (and that IMAP is on for this account), then try again.")
        return 1
    store.save([a for a in accounts if a.address.lower() != address.lower()] + [account])
    print(f"✓ {address} reads and sends. Its password is in {where}.")
    print("  Restart the voice window (or Claude Code) to use it.")
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
