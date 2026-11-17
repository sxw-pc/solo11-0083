"""Command-line entry point: argument parsing and dispatch only."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import AUTHORIZED_USE_NOTICE, __version__
from .cassette import compute_contract_digest, load_cassette, write_signed_cassette
from .config import load_screens_dir, load_session
from .errors import HarvestError
from .logging_util import build_logger
from .mockhost import MockTerminalServer
from .pipeline import run_pipeline
from .signing import LocalSigner, keygen
from .store import Store
from .trust import TrustStore
from .validate_config import load_validation_config

EPILOG = f"合规声明：{AUTHORIZED_USE_NOTICE}"

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="termharvest", description="终端拾屏台：离线、只读、可复现的字符终端屏幕采集管线。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser(
        "serve-mock", help="start the loopback-only mock terminal", description="启动仅绑定 loopback 的确定性 mock 终端。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    serve.add_argument("--scenario", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=0)
    serve.add_argument("--max-connections", type=int, default=8)
    serve.add_argument("--frame-timeout", type=float, default=5.0)
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--log-level", default="INFO")
    serve.add_argument("--log-file")

    run = sub.add_parser("run", help="collect, extract, validate, store and report", description="执行一次只读采集管线。", epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    run.add_argument("--session", required=True)
    run.add_argument("--screens", required=True)
    run.add_argument("--validators", required=True)
    run.add_argument("--db", default="data/termharvest.db")
    run.add_argument("--out", default="data/report")
    run.add_argument("--mode", choices=["live", "replay"], default="live")
    run.add_argument("--cassette")
    run.add_argument("--host")
    run.add_argument("--port", type=int)
    run.add_argument("--evidence-dir")
    run.add_argument("--record-cassette")
    run.add_argument("--trust-store", help="SQLite trust store with enrolled public keys (required for trusted replay)")
    run.add_argument("--trust-policy", choices=["any_trusted_recorder", "two_distinct_recorders"], default="any_trusted_recorder")
    run.add_argument("--require-trusted", action="store_true", help="strict mode: refuse unsigned or unverifiable cassettes")
    run.add_argument("--log-level", default="INFO")
    run.add_argument("--log-file")

    sign = sub.add_parser(
        "sign-cassette",
        help="offline: sign an existing cassette with a local Ed25519 private key",
        description="离线签名：在本机读取私钥，为 v1 cassette 生成 v2 签名包。私钥不会写入 cassette、日志或报告。",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sign.add_argument("--cassette", required=True, help="input v1 JSONL cassette")
    sign.add_argument("--out", required=True, help="output v2 signed cassette")
    sign.add_argument("--session", required=True)
    sign.add_argument("--screens", required=True)
    sign.add_argument("--key-id", required=True)
    sign.add_argument("--private-key-file", required=True, help="local PEM path; read into memory only and never copied to outputs")
    sign.add_argument("--record-seq", type=int, required=True, help="monotonically increasing recording sequence per session")
    sign.add_argument("--captured-by", default="offline-recorder")

    key = sub.add_parser("keygen", help="generate an Ed25519 key pair for offline recording")
    key.add_argument("--key-id", required=True)
    key.add_argument("--trust-store", help="optionally enroll the public key immediately")
    key.add_argument("--out-public", help="write only the public key (base64) to this path")
    key.add_argument("--out-private", help="write the private PEM to this local path (mode 0600)")

    trust = sub.add_parser("trust", help="manage the public-key trust store")
    trust_sub = trust.add_subparsers(dest="trust_command", required=True)
    enroll = trust_sub.add_parser("enroll-key")
    enroll.add_argument("--trust-store", required=True)
    enroll.add_argument("--key-id", required=True)
    enroll.add_argument("--public-key-file", required=True)
    enroll.add_argument("--valid-from")
    enroll.add_argument("--valid-until")
    revoke = trust_sub.add_parser("revoke-key")
    revoke.add_argument("--trust-store", required=True)
    revoke.add_argument("--key-id", required=True)
    revoke.add_argument("--reason")
    listing = trust_sub.add_parser("list-keys")
    listing.add_argument("--trust-store", required=True)
    return parser

def _serve(args: argparse.Namespace) -> int:
    logger = build_logger("termharvest.mock", level=args.log_level, log_file=args.log_file)
    server = MockTerminalServer(args.scenario, host=args.host, port=args.port, max_connections=args.max_connections, frame_timeout=args.frame_timeout, once=args.once, logger=logger)
    port = server.start()
    print(f"MOCK_READY host={server.address[0]} port={port} " f"scenario={server.scenario.scenario_id}", flush=True)
    logger.info("event=mock_listening port=%s once=%s", port, args.once)
    try:
        server.wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0

def _run(args: argparse.Namespace) -> int:
    logger = build_logger("termharvest", level=args.log_level, log_file=args.log_file)
    session = load_session(args.session)
    screens = load_screens_dir(args.screens)
    validation_config = load_validation_config(args.validators)
    store = Store(args.db)
    trust_store = TrustStore(args.trust_store) if args.trust_store else None
    try:
        result = run_pipeline(
            session=session,
            screens=screens,
            validation_config=validation_config,
            store=store,
            output_dir=args.out,
            mode=args.mode,
            cassette_path=args.cassette,
            evidence_dir=args.evidence_dir,
            host_override=args.host,
            port_override=args.port,
            record_cassette=args.record_cassette,
            logger=logger,
            trust_store=trust_store,
            trust_policy=args.trust_policy,
            require_trusted=args.require_trusted,
        )
    finally:
        store.close()
        if trust_store is not None:
            trust_store.close()
    summary = result["summary"]
    print("RUN_RESULT " + json.dumps(summary, ensure_ascii=False, sort_keys=True), flush=True)
    return 0 if summary["status"] == "success" else 1

def _sign_cassette(args: argparse.Namespace) -> int:
    session = load_session(args.session)
    screens = load_screens_dir(args.screens)
    cassette = load_cassette(args.cassette)
    if cassette.session_id != session.session_id:
        raise HarvestError("E_CASSETTE_MISMATCH", "cassette session_id does not match session contract", context={"cassette_session_id": cassette.session_id, "session_id": session.session_id})
    private_pem = Path(args.private_key_file).read_text(encoding="ascii")
    signer = LocalSigner(private_pem, key_id=args.key_id)
    outcome = write_signed_cassette(
        args.out,
        frames=[(item.frame_seq, item.step_id, item.expected_screen, item.frame) for item in cassette.frames],
        session_id=session.session_id,
        record_seq=args.record_seq,
        contract_digest=compute_contract_digest(session, screens),
        captured_by=args.captured_by,
        signers=[signer.signer()],
    )
    print("SIGNED " + json.dumps({**outcome, "session_id": session.session_id}, ensure_ascii=False, sort_keys=True), flush=True)
    return 0

def _keygen(args: argparse.Namespace) -> int:
    private_pem, public_b64 = keygen()
    if args.out_private:
        private_path = Path(args.out_private)
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.write_text(private_pem, encoding="ascii")
        private_path.chmod(0o600)
    if args.out_public:
        public_path = Path(args.out_public)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        public_path.write_text(public_b64 + "\n", encoding="ascii")
    if args.trust_store:
        with TrustStore(args.trust_store) as trust_store:
            trust_store.enroll_key(args.key_id, public_b64)
    # Only the public key is printed.  If no file was requested the private
    # key is deliberately not echoed so it cannot leak into logs or reports.
    print("KEYGEN " + json.dumps({"key_id": args.key_id, "public_key_b64": public_b64, "private_key_written": bool(args.out_private)}, sort_keys=True), flush=True)
    return 0

def _trust(args: argparse.Namespace) -> int:
    with TrustStore(args.trust_store) as trust_store:
        if args.trust_command == "enroll-key":
            public_b64 = Path(args.public_key_file).read_text(encoding="ascii").strip()
            key = trust_store.enroll_key(args.key_id, public_b64, valid_from=args.valid_from, valid_until=args.valid_until)
            print("TRUST_ENROLLED " + json.dumps({"key_id": key.key_id, "public_key_fingerprint": key.fingerprint, "valid_from": key.valid_from, "valid_until": key.valid_until}, sort_keys=True), flush=True)
        elif args.trust_command == "revoke-key":
            trust_store.revoke_key(args.key_id, reason=args.reason)
            print("TRUST_REVOKED " + json.dumps({"key_id": args.key_id}, sort_keys=True), flush=True)
        elif args.trust_command == "list-keys":
            print("TRUST_KEYS " + json.dumps(trust_store.list_keys(), ensure_ascii=False, sort_keys=True), flush=True)
    return 0

def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "serve-mock":
            return _serve(args)
        if args.command == "run":
            return _run(args)
        if args.command == "sign-cassette":
            return _sign_cassette(args)
        if args.command == "keygen":
            return _keygen(args)
        if args.command == "trust":
            return _trust(args)
        return 2
    except HarvestError as exc:
        print(f"ERROR {exc.code}: {exc.message}", file=sys.stderr)
        if exc.context:
            print("CONTEXT " + json.dumps(exc.context, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

if __name__ == "__main__":
    raise SystemExit(main())
