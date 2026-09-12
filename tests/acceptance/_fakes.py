"""Shared real-subprocess fakes for acceptance (no live Codex, no network)."""
import json
import os
import stat

FAKE_CODEX_SRC = r'''#!/usr/bin/env python3
import json, os, sys, time
def log_record(argv):
    rec = os.environ.get("OLO_FAKE_RECORD")
    if rec:
        try:
            with open(rec, "a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "argv": argv,
                    "cwd": os.getcwd(),
                    "cwd_list": sorted(os.listdir(".")),
                    "CODEX_HOME": os.environ.get("CODEX_HOME"),
                    "has_OPENAI_API_KEY": "OPENAI_API_KEY" in os.environ,
                    "has_CODEX_ACCESS_TOKEN": "CODEX_ACCESS_TOKEN" in os.environ,
                }) + "\n")
        except Exception:
            pass
def main():
    argv = sys.argv[1:]
    log_record(argv)
    if not argv:
        return 2
    if argv[0] == "app-server":
        return server()
    if argv[0] == "exec":
        return exec_mode(argv[1:])
    if argv[0] == "login":
        mode = os.environ.get("FAKE_LOGIN_MODE", "ok")
        if argv[1:] == ["status"]:
            return 0 if mode == "ok" else 1
        return 0 if mode == "ok" else 1
    return 2
def rl():
    return sys.stdin.buffer.readline()
def wline(obj, split=False, coalesce_next=None):
    data = (json.dumps(obj) + "\n").encode()
    out = sys.stdout.buffer
    if split and len(data) > 4:
        cut = len(data)//2
        out.write(data[:cut]); out.flush()
        time.sleep(0.05)
        out.write(data[cut:]); out.flush()
    else:
        out.write(data); out.flush()
    if coalesce_next is not None:
        out.write(coalesce_next); out.flush()
def server():
    import sys as _s
    mode = os.environ.get("FAKE_SERVER_MODE", "happy")
    # initialize
    line = rl()
    if not line:
        return 0
    try:
        req = json.loads(line)
    except Exception:
        return 0
    rid = req.get("id", 1)
    if mode == "init-error":
        _s.stdout.buffer.write((json.dumps({"jsonrpc":"2.0","id":rid,"error":{"code":1,"message":"boom secret-token-xyz"}})+"\n").encode()); _s.stdout.buffer.flush()
        time.sleep(0.2)
        return 0
    if mode == "init-exit":
        return 0
    wline({"jsonrpc":"2.0","id":rid,"result":{"ok":True}})
    # flood stderr if requested (chatty server must not deadlock us)
    if os.environ.get("FAKE_FLOOD_STDERR") == "1":
        try:
            _s.stderr.buffer.write(b"n" * 700000); _s.stderr.buffer.flush()
        except Exception:
            pass
    pages_json = os.environ.get("FAKE_MODEL_PAGES")
    pages = json.loads(pages_json) if pages_json else None
    while True:
        line = rl()
        if not line:
            return 0
        if not line.strip():
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        m = req.get("method"); rid = req.get("id")
        if m == "account/read":
            wline({"jsonrpc":"2.0","id":rid,"result":{"account":{"type":"chatgpt"}}})
        elif m == "account/rateLimits/read":
            if mode == "hang-second" and getattr(server, "n", 0) >= 1:
                time.sleep(30)
                return 0
            if mode == "secret-error":
                wline({"jsonrpc":"2.0","id":rid,"error":{"code":-32000,"message":"chatgpt auth Bearer sk-super-secret-xyz"}})
            elif mode == "rpc-error":
                wline({"jsonrpc":"2.0","id":rid,"error":{"code":7,"message":"token abc123 leaked?"}})
            elif mode == "malformed":
                _s.stdout.buffer.write(b"{{{not json\n"); _s.stdout.buffer.flush()
            elif mode == "partial":
                wline({"jsonrpc":"2.0","id":rid,"result":{"v":2}}, split=True)
            elif mode == "coalesced":
                n1 = (json.dumps({"jsonrpc":"2.0","method":"n","params":{}})+"\n").encode()
                n2 = (json.dumps({"jsonrpc":"2.0","id":rid,"result":{"v":1}})+"\n").encode()
                _s.stdout.buffer.write(n1+n2); _s.stdout.buffer.flush()
            elif mode == "hang":
                time.sleep(30)
                return 0
            else:
                wline({"jsonrpc":"2.0","id":rid,"result":{"ordinaryUsageAllowed":True,"rateLimits":{}}})
            server.n = getattr(server, "n", 0) + 1
        elif m == "model/list":
            if pages is not None:
                # Sequential routing by call count (robust to any cursor
                # names); LOOP mode always repeats c1 so the client must
                # fail closed with model-pages-loop; N pages exercise
                # the MAX_MODEL_PAGES cap.
                if os.environ.get("FAKE_MODEL_LOOP") == "1":
                    wline({"jsonrpc":"2.0","id":rid,"result":
                           {"data": [], "nextCursor": "c1"}})
                else:
                    idx = getattr(server, "mi", 0)
                    server.mi = idx + 1
                    if idx < len(pages):
                        wline({"jsonrpc":"2.0","id":rid,"result":pages[idx]})
                    else:
                        wline({"jsonrpc":"2.0","id":rid,"result":{"data":[],"nextCursor":None}})
            else:
                wline({"jsonrpc":"2.0","id":rid,"result":{"data":[{"id":"gpt-5.6-luna","supportedReasoningEfforts":[{"reasoningEffort":"low"}]}],"nextCursor":None}})
        else:
            wline({"jsonrpc":"2.0","id":rid,"result":{}})
def exec_mode(args):
    mode = os.environ.get("FAKE_EXEC_MODE", "success")
    # Always record own pid so the tester can prove reap without pgrep.
    try:
        rec = os.environ.get("OLO_FAKE_RECORD")
        if rec:
            with open(rec, "a", encoding="utf-8") as f:
                f.write(json.dumps({"exec_pid": os.getpid(),
                                    "exec_pgid": os.getpgrp()}) + "\n")
    except Exception:
        pass
    if mode == "orphan":
        # Spawn a lingering grandchild in OUR process group, then exit
        # immediately. Only a pgid-based cleanup (saved at spawn) reaps
        # it; killing just the direct child would orphan it.
        import subprocess as _sp
        try:
            g = _sp.Popen(["sleep", "30"])
            try:
                rec = os.environ.get("OLO_FAKE_RECORD")
                if rec:
                    with open(rec, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"orphan_pid": g.pid}) + "\n")
            except Exception:
                pass
        except Exception:
            pass
        sys.stdout.buffer.write(b'{"type":"done"}\n'); sys.stdout.buffer.flush()
        return 0
    # stdin dash payload: read it if piped
    try:
        if not sys.stdin.isatty():
            data = sys.stdin.buffer.read(65536) if ("-" in args) else None
            rec = os.environ.get("OLO_FAKE_RECORD")
            if rec and ("-" in args):
                with open(rec, "a", encoding="utf-8") as f:
                    f.write(json.dumps({"stdin": (data or b"").decode("utf-8","replace")})+"\n")
    except Exception:
        pass
    if mode == "hang":
        time.sleep(30)
        return 0
    if mode == "big":
        sys.stdout.buffer.write(b"x" * 600000)
        sys.stdout.buffer.flush()
        return 0
    if mode == "nonzero":
        return 3
    sys.stdout.buffer.write(b'{"type":"done"}\n'); sys.stdout.buffer.flush()
    return 0
sys.exit(main())
'''

def write_fake_codex(tmpdir: str) -> str:
    path = os.path.join(tmpdir, "fake-codex")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(FAKE_CODEX_SRC)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
