"""The email connector against in-memory IMAP and SMTP servers.

Run: python -m unittest discover -s voice/tests
"""
import email
import email.policy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
os.environ["GLASSES_EMAIL_STORE"] = "file"

import email_accounts  # noqa: E402
from email_accounts import Account, Mail, Store, cli  # noqa: E402
from email_mcp import Server  # noqa: E402


def message(frm, to, subject, body, html=False, attachment=None, msgid=None):
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = frm, to, subject
    msg["Date"] = "Fri, 31 Oct 2026 09:00:00 -0400"
    msg["Message-ID"] = msgid or f"<{abs(hash(subject))}@example.com>"
    if html:
        msg.set_content(f"<html><body><p>{body}</p><style>p{{}}</style></body></html>", subtype="html")
    else:
        msg.set_content(body)
    if attachment:
        msg.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename=attachment)
    return msg.as_bytes()


class FakeImap:
    """Enough IMAP for email_accounts: folders of (uid, flags, raw message)."""

    def __init__(self, server, fail=False):
        self.server = server
        if fail:
            raise OSError("login failed")
        self.folder = None
        server.connections += 1

    def select(self, folder, readonly=False):
        self.folder = folder.strip('"')
        return "OK", [b"1"]

    def list(self):
        return "OK", [f'(\\HasNoChildren {flag}) "/" "{name}"'.encode() for name, flag in self.server.special.items()]

    def uid(self, command, *args):
        box = self.server.folders.setdefault(self.folder, [])
        if command == "SEARCH":
            self.server.searches.append(args)
            unread = "UNSEEN" in args or (args[0] == "X-GM-RAW" and "is:unread" in args[1])
            hits = [m for m in box if not unread or "\\Seen" not in m["flags"]]
            if "TEXT" in args:
                needle = args[args.index("TEXT") + 1].strip('"').lower()
                hits = [m for m in hits if needle in m["raw"].decode(errors="replace").lower()]
            return "OK", [" ".join(str(m["uid"]) for m in hits).encode()]
        uid = int(args[0])
        found = next(m for m in box if m["uid"] == uid)
        if command == "STORE":
            found["flags"].add("\\Seen")
            return "OK", [b""]
        spec = args[1]
        flags = " ".join(sorted(found["flags"]))
        if "BODY.PEEK[HEADER]" in spec:
            header = found["raw"].split(b"\n\n", 1)[0] + b"\n\n"
            return "OK", [(f"1 (UID {uid} FLAGS ({flags}) RFC822.SIZE {len(found['raw'])} BODY[HEADER] {{{len(header)}}}".encode(), header), b")"]
        return "OK", [(f"1 (UID {uid} BODY[] {{{len(found['raw'])}}}".encode(), found["raw"]), b")"]

    def append(self, folder, flags, date, raw):
        box = self.server.folders.setdefault(folder.strip('"'), [])
        box.append({"uid": len(box) + 1, "flags": {flags.strip("()")}, "raw": raw})
        return "OK", [b""]

    def logout(self):
        return "BYE", [b""]


class FakeSmtp:
    def __init__(self, server):
        self.server = server

    def send_message(self, msg):
        self.server.sent.append(msg)

    def quit(self):
        pass


class FakeServer:
    def __init__(self, special):
        self.folders = {"INBOX": []}
        self.special = special
        self.searches = []
        self.sent = []
        self.connections = 0

    def add(self, raw, seen=False, folder="INBOX"):
        box = self.folders.setdefault(folder, [])
        box.append({"uid": len(box) + 1, "flags": {"\\Seen"} if seen else set(), "raw": raw})


class EmailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.gmail = Account("me@gmail.com", "imap.gmail.com", 993, "smtp.gmail.com", 465, provider="gmail")
        self.icloud = Account("me@icloud.com", "imap.mail.me.com", 993, "smtp.mail.me.com", 587,
                              provider="icloud", name="Me Myself")
        self.store.save([self.gmail, self.icloud])
        self.store.set_password("me@gmail.com", "gmail-app-password")
        self.store.set_password("me@icloud.com", "icloud-app-password")
        self.servers = {
            "me@gmail.com": FakeServer({"[Gmail]/Drafts": "\\Drafts", "[Gmail]/Sent Mail": "\\Sent"}),
            "me@icloud.com": FakeServer({"Drafts": "\\Drafts", "Sent Messages": "\\Sent"}),
        }
        self.broken = set()
        self.passwords = []

        def imap(account, password):
            self.passwords.append(password)
            return FakeImap(self.servers[account.address], fail=account.address in self.broken)

        self.mail = Mail(self.store, imap=imap, smtp=lambda a, p: FakeSmtp(self.servers[a.address]))
        self.servers["me@gmail.com"].add(message("Jessie <j@vendor.com>", "me@gmail.com", "Kiosk shipping",
                                                 "The kiosk ships Monday.", html=True))
        self.servers["me@gmail.com"].add(message("news@site.com", "me@gmail.com", "Weekly digest", "Old news"), seen=True)
        self.servers["me@icloud.com"].add(message("Bank <b@bank.com>", "me@icloud.com", "Statement ready",
                                                  "Your statement is ready.", attachment="statement.pdf",
                                                  msgid="<stmt-1@bank.com>"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_searches_every_account_at_once(self):
        results = self.mail.search(unread_only=True)
        self.assertEqual(set(results), {"me@gmail.com", "me@icloud.com"})
        gmail = results["me@gmail.com"]
        self.assertEqual([m["subject"] for m in gmail], ["Kiosk shipping"])
        self.assertIn("The kiosk ships Monday.", gmail[0]["snippet"])
        self.assertNotIn("<p>", gmail[0]["snippet"])
        self.assertTrue(gmail[0]["unread"])
        # Gmail accounts search with Gmail's own syntax
        self.assertEqual(self.servers["me@gmail.com"].searches[0][0], "X-GM-RAW")
        self.assertIn("is:unread", self.servers["me@gmail.com"].searches[0][1])
        self.assertEqual(results["me@icloud.com"][0]["subject"], "Statement ready")
        self.assertIn("UNSEEN", self.servers["me@icloud.com"].searches[0])

    def test_one_broken_account_does_not_hide_the_others(self):
        self.broken.add("me@icloud.com")
        results = self.mail.search()
        self.assertIn("error", results["me@icloud.com"])
        self.assertEqual(len(results["me@gmail.com"]), 2)

    def test_reads_a_message(self):
        found = self.mail.search(account="icloud")["me@icloud.com"][0]
        msg = self.mail.read(found["id"])
        self.assertEqual(msg["body"], "Your statement is ready.")
        self.assertEqual(msg["attachments"], ["statement.pdf"])
        self.assertEqual(msg["account"], "me@icloud.com")

    def test_sends_from_any_account_and_keeps_a_copy(self):
        found = self.mail.search(account="me@icloud.com")["me@icloud.com"][0]
        result = self.mail.send("icloud", "b@bank.com", "", "Thanks, got it.", reply_to_id=found["id"])
        self.assertEqual(result, "sent from me@icloud.com to b@bank.com")
        sent = self.servers["me@icloud.com"].sent[0]
        self.assertEqual(sent["From"], "Me Myself <me@icloud.com>")
        self.assertEqual(sent["Subject"], "Re: Statement ready")
        self.assertEqual(sent["In-Reply-To"], "<stmt-1@bank.com>")
        # iCloud doesn't keep mail sent over SMTP, so a copy goes in Sent
        self.assertEqual(len(self.servers["me@icloud.com"].folders["Sent Messages"]), 1)
        self.assertEqual(self.passwords[-1], "icloud-app-password")

    def test_gmail_keeps_its_own_sent_copy(self):
        self.mail.send("me@gmail.com", "j@vendor.com", "Hello", "Hi Jessie")
        self.assertEqual(len(self.servers["me@gmail.com"].sent), 1)
        self.assertNotIn("[Gmail]/Sent Mail", self.servers["me@gmail.com"].folders)

    def test_drafts_go_in_the_drafts_folder(self):
        self.mail.draft("gmail", "j@vendor.com", "Re: Kiosk", "Sounds good.")
        drafts = self.servers["me@gmail.com"].folders["[Gmail]/Drafts"]
        self.assertEqual(email.message_from_bytes(drafts[0]["raw"], policy=email.policy.default)["To"], "j@vendor.com")

    def test_ambiguous_or_unknown_accounts_are_refused(self):
        with self.assertRaisesRegex(ValueError, "me@gmail.com, me@icloud.com"):
            self.mail.account("me")
        with self.assertRaises(ValueError):
            self.mail.account("work")

    def test_mcp_tools(self):
        server = Server(self.mail)
        init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        self.assertIsNone(server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        names = [t["name"] for t in server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertEqual(names, ["list_accounts", "search_email", "read_email", "mark_read", "draft_email", "send_email"])
        call = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "search_email", "arguments": {"query": "kiosk", "account": "gmail"}}})
        self.assertIn("Kiosk shipping", call["result"]["content"][0]["text"])
        bad = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                             "params": {"name": "read_email", "arguments": {"id": "nobody@x.com|INBOX|1"}}})
        self.assertTrue(bad["result"]["isError"])

    def test_mcp_server_over_stdio(self):
        env = {**os.environ, "GLASSES_DATA_DIR": self.tmp.name}
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_accounts", "arguments": {}}},
        ]
        out = subprocess.run([sys.executable, str(HERE.parent / "email_mcp.py")], env=env, capture_output=True,
                             text=True, timeout=30, input="".join(json.dumps(r) + "\n" for r in requests)).stdout
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], [1, 2])
        accounts = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual([a["address"] for a in accounts], ["me@gmail.com", "me@icloud.com"])


class EmailCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.ok = True

        class CheckingMail(Mail):
            def check(inner, account):
                return None if self.ok else "reading mail failed: authentication failed"

        self.mail = CheckingMail(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv, password="abcd efgh ijkl mnop"):
        return cli(list(argv), store=self.store, mail=self.mail, ask=lambda prompt: password)

    def test_adds_a_gmail_account(self):
        self.assertEqual(self.run_cli("add", "Partner@Gmail.com", "--name", "Raph"), 0)
        account = self.store.accounts()[0]
        self.assertEqual((account.address, account.provider, account.imap_host, account.name),
                         ("Partner@Gmail.com", "gmail", "imap.gmail.com", "Raph"))
        # app passwords are shown in groups of four; the spaces aren't part of it
        self.assertEqual(self.store.password("Partner@Gmail.com"), "abcdefghijklmnop")
        self.assertEqual(oct(os.stat(self.store.secrets_file).st_mode & 0o777), "0o600")

    def test_a_password_that_does_not_log_in_is_not_kept(self):
        self.ok = False
        self.assertEqual(self.run_cli("add", "me@icloud.com"), 1)
        self.assertEqual(self.store.accounts(), [])
        self.assertIsNone(self.store.password("me@icloud.com"))

    def test_outlook_needs_another_way(self):
        self.assertEqual(self.run_cli("add", "me@outlook.com"), 1)
        self.assertEqual(self.store.accounts(), [])

    def test_other_domains_need_their_servers(self):
        original = email_accounts.mx_provider
        email_accounts.mx_provider = lambda domain: None
        try:
            self.assertEqual(self.run_cli("add", "raph@mykiosks.ca"), 1)
            self.assertEqual(self.run_cli("add", "raph@mykiosks.ca", "--imap", "mail.mykiosks.ca", "--smtp", "mail.mykiosks.ca:587"), 0)
        finally:
            email_accounts.mx_provider = original
        account = self.store.accounts()[0]
        self.assertEqual((account.imap_port, account.smtp_host, account.smtp_port), (993, "mail.mykiosks.ca", 587))

    def test_custom_domains_on_google_are_found_by_mx(self):
        original = email_accounts.mx_provider
        email_accounts.mx_provider = lambda domain: "gmail"
        try:
            self.assertEqual(self.run_cli("add", "raph@company.com"), 0)
        finally:
            email_accounts.mx_provider = original
        self.assertEqual(self.store.accounts()[0].provider, "gmail")

    def test_remove(self):
        self.run_cli("add", "me@gmail.com")
        self.assertEqual(self.run_cli("remove", "me@gmail.com"), 0)
        self.assertEqual(self.store.accounts(), [])
        self.assertIsNone(self.store.password("me@gmail.com"))


if __name__ == "__main__":
    unittest.main()
