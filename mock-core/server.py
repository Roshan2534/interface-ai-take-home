#!/usr/bin/env python3
"""Horizon Credit Union - Member Servicing Console (training host).

Intentionally hostile stand-in for a legacy core: framesets, nested tables,
no element ids or test ids, mixed GET/POST, and real business exceptions.

  python3 server.py            # http://127.0.0.1:7878/
  python3 server.py --port 7878

Demo operator: TELLER01 / train

Seed members:
  12345  happy path (lookup + open sub-account)
  10001  second member, for parameterized replay
  88888  viewable, not authorized to open a sub-account
  77777  address-change interstitial before detail
  99999  record not found
"""

from __future__ import annotations

import argparse
import html
import secrets
import time
from copy import deepcopy
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST_NAME = "HCU-CICS2"
REGION = "PROD2"
MIN_OPENING_DEPOSIT = 25.00
SESSION_TTL_SEC = 45 * 60

SEED_MEMBERS = {
    "12345": {
        "name": "JANE A OKONKWO",
        "status": "ACTIVE",
        "branch": "0042 AUSTIN",
        "opened": "03/14/2019",
        "restricted": False,
        "needs_address_ack": False,
        "accounts": [
            {
                "id": "80-12345-01",
                "type": "SHARE DRAFT",
                "product": "CHECKING",
                "bal": 890.44,
            },
            {
                "id": "80-12345-02",
                "type": "REGULAR SHARE",
                "product": "SAVINGS",
                "bal": 4250.18,
            },
        ],
    },
    "10001": {
        "name": "ROBERT M CHEN",
        "status": "ACTIVE",
        "branch": "0018 DALLAS",
        "opened": "11/02/2016",
        "restricted": False,
        "needs_address_ack": False,
        "accounts": [
            {
                "id": "80-10001-01",
                "type": "SHARE DRAFT",
                "product": "CHECKING",
                "bal": 210.07,
            },
            {
                "id": "80-10001-02",
                "type": "REGULAR SHARE",
                "product": "SAVINGS",
                "bal": 1200.00,
            },
        ],
    },
    "88888": {
        "name": "RESTRICTED TRUST UF-88",
        "status": "RESTRICTED",
        "branch": "0001 HQ",
        "opened": "01/09/2008",
        "restricted": True,
        "needs_address_ack": False,
        "accounts": [
            {
                "id": "80-88888-02",
                "type": "REGULAR SHARE",
                "product": "SAVINGS",
                "bal": 88120.55,
            },
        ],
    },
    "77777": {
        "name": "MARIA L SANTOS",
        "status": "ACTIVE",
        "branch": "0042 AUSTIN",
        "opened": "06/21/2021",
        "restricted": False,
        "needs_address_ack": True,
        "accounts": [
            {
                "id": "80-77777-02",
                "type": "REGULAR SHARE",
                "product": "SAVINGS",
                "bal": 640.12,
            },
        ],
    },
}

PRODUCTS = [
    ("REGULAR SHARE", "SAVINGS"),
    ("MONEY MARKET", "MONEY MARKET"),
    ("SHARE CERT", "CERTIFICATE"),
]

# sid -> session dict
SESSIONS: dict[str, dict] = {}
MEMBERS: dict[str, dict] = {}
NEXT_CONF = 44190


def reset_world() -> None:
    global MEMBERS, NEXT_CONF
    MEMBERS = deepcopy(SEED_MEMBERS)
    NEXT_CONF = 44190


reset_world()


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def money(value: float) -> str:
    return f"{value:,.2f}"


def css() -> str:
    return """
    body { margin:0; background:#c0c0c8; color:#000;
           font-family: Tahoma, "MS Sans Serif", Verdana, sans-serif; font-size:11px; }
    a { color:#0000ee; }
    a.dead { color:#666; text-decoration:none; }
    .navy { background:#000080; color:#fff; font-weight:bold; padding:5px 8px;
            font-size:12px; }
    .strip { background:#000080; color:#fff; padding:3px 6px; }
    .host { font-family:"Courier New", monospace; background:#003300; color:#66ff66;
            padding:1px 6px; }
    .err { background:#ffff66; color:#990000; font-weight:bold;
           border:2px solid #990000; padding:6px; }
    .ok { background:#ccffcc; border:1px solid #006600; padding:6px; }
    .warn { background:#ffcc66; border:1px solid #996600; padding:6px; }
    table.grid { background:#fff; border:1px solid #404040; }
    table.grid td, table.grid th { border:1px solid #808080; padding:3px 6px;
                                   font-size:11px; }
    table.grid th { background:#d4d0c8; }
    input, select { font-family: Tahoma, sans-serif; font-size:11px; }
    input.mono { font-family:"Courier New", monospace; }
    .menu { background:#d4d0c8; }
    .menu a { display:block; padding:4px 8px; text-decoration:none; color:#000;
              border-bottom:1px solid #808080; }
    .menu a:hover { background:#000080; color:#fff; }
    .ftr { color:#404040; font-size:10px; padding:6px; }
    iframe.spool { width:100%; height:92px; background:#fff; border:1px inset #808080; }
    """


def shell(title: str, body: str, extra_head: str = "") -> bytes:
    doc = f"""<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN">
<html>
<head>
<meta http-equiv="Content-Type" content="text/html; charset=utf-8">
<title>{esc(title)}</title>
<style>{css()}</style>
{extra_head}
</head>
<body>
{body}
</body>
</html>
"""
    return doc.encode("utf-8")


def new_session(operator: str) -> str:
    sid = secrets.token_hex(16)
    SESSIONS[sid] = {
        "operator": operator,
        "created": time.time(),
        "last": time.time(),
        "member_no": None,
        "ofac_ack": False,
        "address_ack": set(),
        "fault": None,
    }
    return sid


def get_session(handler: BaseHTTPRequestHandler) -> dict | None:
    raw = handler.headers.get("Cookie", "")
    cookie = SimpleCookie()
    try:
        cookie.load(raw)
    except Exception:
        return None
    morsel = cookie.get("HCUSESS")
    if not morsel:
        return None
    sess = SESSIONS.get(morsel.value)
    if not sess:
        return None
    if time.time() - sess["last"] > SESSION_TTL_SEC:
        SESSIONS.pop(morsel.value, None)
        return None
    sess["last"] = time.time()
    sess["sid"] = morsel.value
    return sess


def set_cookie_headers(sid: str) -> list[str]:
    return [f"HCUSESS={sid}; Path=/; HttpOnly; SameSite=Lax"]


def parse_body(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or 0)
    raw = handler.rfile.read(length) if length else b""
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in parsed.items()}


def query(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    parsed = parse_qs(urlparse(handler.path).query, keep_blank_values=True)
    return {k: (v[-1] if v else "") for k, v in parsed.items()}


def normalize_memno(raw: str) -> str:
    return "".join(ch for ch in (raw or "") if ch.isdigit())


def savings_balance(member: dict) -> float | None:
    for acct in member["accounts"]:
        if acct["product"] == "SAVINGS":
            return acct["bal"]
    return None


def next_account_id(memno: str, member: dict) -> str:
    seq = len(member["accounts"]) + 1
    return f"80-{memno}-{seq:02d}"


def next_conf() -> str:
    global NEXT_CONF
    NEXT_CONF += 1
    return f"CUS-{NEXT_CONF}"


def login_page(error: str = "") -> bytes:
    err = f'<tr><td colspan="2"><div class="err">{esc(error)}</div></td></tr>' if error else ""
    body = f"""
<table width="100%" cellspacing="0" cellpadding="0">
  <tr><td class="navy">HORIZON CREDIT UNION &nbsp;|&nbsp; MEMBER SERVICING CONSOLE</td></tr>
</table>
<br>
<center>
<table cellpadding="8" cellspacing="0" border="1" bgcolor="#d4d0c8">
  <tr><td>
    <table cellpadding="3" cellspacing="0">
      <tr><td colspan="2"><b>SIGN ON</b> &nbsp; <span class="host">{HOST_NAME} {REGION}</span></td></tr>
      {err}
      <form method="POST" action="/login" target="_top">
      <tr>
        <td>OPERATOR ID</td>
        <td><input class="mono" type="text" name="OPID" size="12" maxlength="8"></td>
      </tr>
      <tr>
        <td>PASSWORD</td>
        <td><input class="mono" type="password" name="PSWD" size="12"></td>
      </tr>
      <tr>
        <td colspan="2">
          <input type="submit" value="  ENTER  ">
          &nbsp; <font color="#666">training: TELLER01 / train</font>
        </td>
      </tr>
      </form>
    </table>
  </td></tr>
</table>
<p class="ftr">Unauthorized access is prohibited. Training host - dummy data only.</p>
</center>
"""
    return shell("HCU Sign On", body)


def frameset() -> bytes:
    doc = """<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Frameset//EN">
<html>
<head><title>HCU Member Servicing</title></head>
<frameset cols="178,*" border="1" frameborder="1" framespacing="1">
  <frame src="/frame/menu" name="menu" scrolling="auto" noresize>
  <frameset rows="46,*">
    <frame src="/frame/banner" name="banner" scrolling="no" noresize>
    <frame src="/inquiry" name="main" scrolling="auto">
  </frameset>
</frameset>
<noframes><body>This console requires frames.</body></noframes>
</html>
"""
    return doc.encode("utf-8")


def menu_page() -> bytes:
    body = """
<div class="navy">FUNCTIONS</div>
<div class="menu">
  <a href="/inquiry" target="main">Member Inquiry</a>
  <a href="/open" target="main">Open Sub-Account</a>
  <a href="/inquiry" target="main" class="dead">Batch Reports</a>
  <a href="/inquiry" target="main" class="dead">Wire Transfer</a>
  <a href="/training/reset" target="main">Reset Training Data</a>
  <a href="/logout" target="_top">Log Off</a>
</div>
<p class="ftr">PF5 Inquire<br>PF9 Open<br>Clear Logoff</p>
"""
    return shell("menu", body)


def banner_page(sess: dict) -> bytes:
    mem = sess.get("member_no") or "-----"
    body = f"""
<table width="100%" cellspacing="0" cellpadding="0">
  <tr>
    <td class="strip">
      HORIZON CU &nbsp; MEMBER SERVICING &nbsp;
      <span class="host">{HOST_NAME}/{REGION}</span>
      &nbsp; OP {esc(sess["operator"])}
      &nbsp; MEM {esc(mem)}
    </td>
  </tr>
</table>
"""
    return shell("banner", body)


def inquiry_form(error: str = "", memno: str = "") -> bytes:
    err = f'<div class="err">{esc(error)}</div><br>' if error else ""
    body = f"""
<table width="100%" cellspacing="0" cellpadding="4">
  <tr><td><b>CIF INQUIRY</b> &nbsp; - enter member number and press Inquire</td></tr>
</table>
{err}
<form method="POST" action="/inquiry">
<table cellpadding="4" cellspacing="0" border="1" bgcolor="#d4d0c8">
  <tr>
    <td>MEM NO</td>
    <td><input class="mono" type="text" name="MEMNO" size="12" maxlength="12" value="{esc(memno)}"></td>
    <td><input type="submit" name="ACT" value="  INQUIRE  "></td>
  </tr>
</table>
</form>
<p class="ftr">Known training members: 12345, 10001, 88888 (restricted), 77777 (CIF warning), 99999 (not on file).</p>
"""
    return shell("CIF Inquiry", body)


def address_interstitial(memno: str) -> bytes:
    body = f"""
<div class="warn">
  <b>HOST MESSAGE 12E</b><br>
  CIF ADDRESS CHANGE PENDING FOR MEMBER {esc(memno)}.<br>
  Review is not complete. Continue only if you accept the current address of record.
</div>
<br>
<form method="POST" action="/inquiry/address-ack">
  <input type="hidden" name="MEMNO" value="{esc(memno)}">
  <input type="submit" name="ACT" value="  CONTINUE  ">
  &nbsp;
  <input type="submit" name="ACT" value="  CANCEL  ">
</form>
"""
    return shell("Host Message", body)


def member_detail(memno: str, member: dict, notice: str = "") -> bytes:
    notice_html = f'<div class="ok">{esc(notice)}</div><br>' if notice else ""
    rows = []
    for acct in member["accounts"]:
        rows.append(
            "<tr>"
            f"<td>{esc(acct['id'])}</td>"
            f"<td>{esc(acct['type'])}</td>"
            f"<td>{esc(acct['product'])}</td>"
            f"<td align='right'>{esc(money(acct['bal']))}</td>"
            "</tr>"
        )
    sav = savings_balance(member)
    sav_line = (
        f"CURRENT SAVINGS BALANCE&nbsp;&nbsp;<b>{esc(money(sav))}</b>"
        if sav is not None
        else "NO SAVINGS SHARE ON FILE"
    )
    body = f"""
{notice_html}
<table width="100%" cellpadding="3" cellspacing="0" border="1" bgcolor="#e8e8e8">
  <tr bgcolor="#000080"><td colspan="4"><font color="#ffffff"><b>MEMBER {esc(memno)}</b></font></td></tr>
  <tr>
    <td>NAME</td><td><b>{esc(member['name'])}</b></td>
    <td>STATUS</td><td>{esc(member['status'])}</td>
  </tr>
  <tr>
    <td>BRANCH</td><td>{esc(member['branch'])}</td>
    <td>OPENED</td><td>{esc(member['opened'])}</td>
  </tr>
</table>
<br>
<table class="grid" cellspacing="0">
  <tr><th>ACCT</th><th>TYPE</th><th>PRODUCT</th><th>BAL</th></tr>
  {''.join(rows)}
</table>
<br>
<table cellpadding="4" bgcolor="#d4d0c8" border="1"><tr><td>{sav_line}</td></tr></table>
<br>
<form method="GET" action="/open">
  <input type="submit" value="  OPEN SUB-ACCOUNT  ">
</form>
"""
    return shell(f"Member {memno}", body)


def ofac_page(memno: str) -> bytes:
    body = f"""
<div class="warn">
  <b>OFAC SCAN</b><br>
  Watchlist scan complete for member {esc(memno)}. No matches.<br>
  You must acknowledge before opening a sub-account.
</div>
<br>
<form method="POST" action="/open/ofac">
  <input type="submit" name="ACT" value="  CONTINUE  ">
</form>
"""
    return shell("OFAC Scan", body)


def open_form(sess: dict, error: str = "", deposit: str = "25.00") -> bytes:
    memno = sess.get("member_no")
    if not memno or memno not in MEMBERS:
        return inquiry_form(error="Select a member with Inquiry before opening a sub-account.")
    member = MEMBERS[memno]
    if member["restricted"]:
        body = f"""
<div class="err">
  SECURITY VIOLATION &nbsp; OP {esc(sess['operator'])} is not authorized for
  function OPN-SUB on member {esc(memno)}.<br>
  Contact supervisor. This is a permission denial, not a host outage.
</div>
<p><a href="/inquiry">Return to inquiry</a></p>
"""
        return shell("Not authorized", body)

    err = f'<div class="err">{esc(error)}</div><br>' if error else ""
    options = "\n".join(
        f'<option value="{esc(code)}">{esc(code)} - {esc(label)}</option>'
        for code, label in PRODUCTS
    )
    body = f"""
<table width="100%" cellpadding="3"><tr><td><b>OPEN SUB-ACCOUNT</b> &nbsp; MEMBER {esc(memno)} {esc(member['name'])}</td></tr></table>
{err}
<form method="POST" action="/open">
<table cellpadding="4" cellspacing="0" border="1" bgcolor="#d4d0c8">
  <tr>
    <td>PRODUCT</td>
    <td><select name="PROD">{options}</select></td>
  </tr>
  <tr>
    <td>OPENING DEPOSIT</td>
    <td><input class="mono" type="text" name="DEPAMT" size="10" value="{esc(deposit)}"> &nbsp; min {MIN_OPENING_DEPOSIT:.2f}</td>
  </tr>
  <tr>
    <td>NICKNAME</td>
    <td><input type="text" name="NICK" size="18"></td>
  </tr>
  <tr>
    <td colspan="2">
      <input type="submit" name="ACT" value="  SUBMIT OPEN  ">
      &nbsp; <a href="/inquiry">Cancel</a>
    </td>
  </tr>
</table>
</form>
<p class="ftr">Opening a share is treated as a risky posting. Confirmation is required.</p>
"""
    return shell("Open Sub-Account", body)


def confirmation_page(memno: str, member: dict, acct: dict, conf: str) -> bytes:
    sav = savings_balance(member)
    spool = (
        f"HCU SPOOL  {HOST_NAME}  CONF {conf}  MEM {memno}  "
        f"NEW {acct['id']}  {acct['product']}  BAL {money(acct['bal'])}"
    )
    body = f"""
<div class="ok"><b>SUB-ACCOUNT OPENED</b></div>
<br>
<table cellpadding="4" cellspacing="0" border="1" bgcolor="#e8e8e8">
  <tr><td>MEMBER</td><td>{esc(memno)} {esc(member['name'])}</td></tr>
  <tr><td>NEW ACCT</td><td><b>{esc(acct['id'])}</b></td></tr>
  <tr><td>PRODUCT</td><td>{esc(acct['type'])} / {esc(acct['product'])}</td></tr>
  <tr><td>OPENING BAL</td><td>{esc(money(acct['bal']))}</td></tr>
  <tr><td>SAVINGS BAL</td><td>{esc(money(sav) if sav is not None else 'n/a')}</td></tr>
  <tr><td>CONFIRMATION</td><td><b>{esc(conf)}</b></td></tr>
</table>
<br>
HOST PRINT PREVIEW<br>
<iframe class="spool" name="spool" src="/print/spool?t={esc(spool)}"></iframe>
<br><br>
<a href="/inquiry">New inquiry</a>
"""
    return shell("Sub-Account Opened", body)


def timeout_page() -> bytes:
    body = """
<div class="err">
  SESSION TIMEOUT - host dropped the terminal session.<br>
  Press OK to sign on again.
</div>
<br>
<form method="GET" action="/" target="_top">
  <input type="submit" value="  OK  ">
</form>
"""
    return shell("Session Timeout", body)


def spool_page(text: str) -> bytes:
    body = f"""
<pre style="font-family:'Courier New',monospace;font-size:11px;margin:8px;">{esc(text)}</pre>
"""
    return shell("spool", body)


class Handler(BaseHTTPRequestHandler):
    server_version = "HCUHost/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        sys_stderr = __import__("sys").stderr
        sys_stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8",
              extra_headers: list[str] | None = None, cookies: list[str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for h in extra_headers or []:
            k, _, v = h.partition(": ")
            self.send_header(k, v)
        for c in cookies or []:
            self.send_header("Set-Cookie", c)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, cookies: list[str] | None = None) -> None:
        body = f'<a href="{esc(location)}">continue</a>'.encode()
        self._send(302, body, extra_headers=[f"Location: {location}"], cookies=cookies)

    def _need_sess(self) -> dict | None:
        sess = get_session(self)
        if not sess:
            self._redirect("/")
            return None
        q = query(self)
        if q.get("fault") == "timeout" or sess.get("fault") == "timeout":
            self._send(200, timeout_page())
            return None
        return sess

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        q = query(self)

        if path == "/health":
            self._send(200, b'{"ok":true}\n', "application/json")
            return
        if path == "/":
            self._send(200, login_page())
            return
        if path == "/logout":
            sess = get_session(self)
            if sess:
                SESSIONS.pop(sess.get("sid", ""), None)
            self._redirect("/", cookies=["HCUSESS=; Path=/; Max-Age=0"])
            return
        if path == "/timeout":
            self._send(200, timeout_page())
            return
        if path == "/print/spool":
            self._send(200, spool_page(q.get("t", "")))
            return

        sess = self._need_sess()
        if sess is None:
            return

        if path == "/console":
            self._send(200, frameset())
            return
        if path == "/frame/menu":
            self._send(200, menu_page())
            return
        if path == "/frame/banner":
            self._send(200, banner_page(sess))
            return
        if path == "/inquiry":
            memno = normalize_memno(q.get("MEMNO", ""))
            if memno:
                self._render_inquiry(sess, memno)
                return
            self._send(200, inquiry_form())
            return
        if path == "/open":
            self._render_open(sess)
            return
        if path == "/training/reset":
            reset_world()
            sess["member_no"] = None
            sess["ofac_ack"] = False
            sess["address_ack"] = set()
            self._send(
                200,
                inquiry_form(error="Training data reset. Members restored to seed."),
            )
            return

        self._send(404, shell("Not found", "<div class='err'>Program not found.</div>"))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        form = parse_body(self)

        if path == "/login":
            opid = (form.get("OPID") or "").strip().upper()
            pswd = form.get("PSWD") or ""
            if opid != "TELLER01" or pswd != "train":
                self._send(200, login_page("Sign-on failed. Invalid operator or password."))
                return
            sid = new_session(opid)
            self._redirect("/console", cookies=set_cookie_headers(sid))
            return

        sess = self._need_sess()
        if sess is None:
            return

        if path == "/inquiry":
            memno = normalize_memno(form.get("MEMNO", ""))
            self._render_inquiry(sess, memno)
            return
        if path == "/inquiry/address-ack":
            act = (form.get("ACT") or "").upper()
            memno = normalize_memno(form.get("MEMNO", ""))
            if "CANCEL" in act:
                sess["member_no"] = None
                self._send(200, inquiry_form())
                return
            sess.setdefault("address_ack", set()).add(memno)
            self._render_inquiry(sess, memno, skip_address=True)
            return
        if path == "/open/ofac":
            sess["ofac_ack"] = True
            self._send(200, open_form(sess))
            return
        if path == "/open":
            self._do_open(sess, form)
            return

        self._send(404, shell("Not found", "<div class='err'>Program not found.</div>"))

    def _render_inquiry(self, sess: dict, memno: str, skip_address: bool = False) -> None:
        if not memno:
            self._send(200, inquiry_form(error="Member number is required."))
            return
        if len(memno) < 4:
            self._send(
                200,
                inquiry_form(error="Invalid member number. Must be at least 4 digits.", memno=memno),
            )
            return
        member = MEMBERS.get(memno)
        if member is None:
            self._send(
                200,
                inquiry_form(error=f"RECORD NOT FOUND - member {memno} is not on file.", memno=memno),
            )
            return
        if member.get("needs_address_ack") and memno not in sess.get("address_ack", set()) and not skip_address:
            sess["member_no"] = memno
            self._send(200, address_interstitial(memno))
            return
        sess["member_no"] = memno
        sess["ofac_ack"] = False
        self._send(200, member_detail(memno, member))

    def _render_open(self, sess: dict) -> None:
        memno = sess.get("member_no")
        if not memno or memno not in MEMBERS:
            self._send(200, inquiry_form(error="Select a member with Inquiry before opening a sub-account."))
            return
        if MEMBERS[memno]["restricted"]:
            self._send(200, open_form(sess))
            return
        if not sess.get("ofac_ack"):
            self._send(200, ofac_page(memno))
            return
        self._send(200, open_form(sess))

    def _do_open(self, sess: dict, form: dict) -> None:
        memno = sess.get("member_no")
        if not memno or memno not in MEMBERS:
            self._send(200, inquiry_form(error="Select a member with Inquiry before opening a sub-account."))
            return
        member = MEMBERS[memno]
        if member["restricted"]:
            self._send(200, open_form(sess))
            return
        if not sess.get("ofac_ack"):
            self._send(200, ofac_page(memno))
            return

        prod_code = (form.get("PROD") or "").strip()
        product = next((p for p in PRODUCTS if p[0] == prod_code), None)
        if product is None:
            self._send(200, open_form(sess, error="Product is required."))
            return
        raw_amt = (form.get("DEPAMT") or "").strip().replace(",", "")
        try:
            amount = float(raw_amt)
        except ValueError:
            self._send(200, open_form(sess, error="Opening deposit is not a valid amount.", deposit=raw_amt))
            return
        if amount < MIN_OPENING_DEPOSIT:
            self._send(
                200,
                open_form(
                    sess,
                    error=f"VALIDATION - opening deposit must be at least {MIN_OPENING_DEPOSIT:.2f}.",
                    deposit=raw_amt,
                ),
            )
            return

        acct = {
            "id": next_account_id(memno, member),
            "type": product[0],
            "product": product[1],
            "bal": amount,
        }
        member["accounts"].append(acct)
        conf = next_conf()
        sess["ofac_ack"] = False
        self._send(200, confirmation_page(memno, member, acct, conf))


def main() -> None:
    parser = argparse.ArgumentParser(description="HCU member servicing training host")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7878)
    args = parser.parse_args()
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"HCU training host on http://{args.host}:{args.port}/", flush=True)
    print("Operator TELLER01 / train", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
