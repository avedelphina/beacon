"""TLS without an overlay network: an agent behind a TLS terminator, with a
certificate from the operator's own CA. Beacon's A2A calls trust that CA
when BEACON_CA_FILE names it, on top of the public roots, and refuse the
endpoint without it. A real handshake against a local https server."""

import datetime
import json
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend import bus, mesh
from backend.schemas import Agent


def _cert(subject: str, issuer_key, issuer_name, key, ca: bool):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (x509.CertificateBuilder()
               .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .issuer_name(issuer_name or x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=1)).not_valid_after(now + datetime.timedelta(hours=1))
               .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
               # Python 3.13+ verifies strictly and wants both key identifiers.
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
                              critical=False)
               .add_extension(x509.KeyUsage(digital_signature=not ca, key_cert_sign=ca, crl_sign=ca,
                                            content_commitment=False, key_encipherment=False, data_encipherment=False,
                                            key_agreement=False, encipher_only=False, decipher_only=False),
                              critical=True))
    if not ca:
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


@pytest.fixture
def tls_agent(tmp_path):
    ca_key, key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    ca = _cert("Operator CA", ca_key, None, ca_key, ca=True)
    leaf = _cert("localhost", ca_key, ca.subject, key, ca=False)
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (tmp_path / "leaf.key").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                          serialization.NoEncryption()))

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            body = json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"task": {
                "id": "t1", "contextId": "c1", "status": {"state": "TASK_STATE_COMPLETED",
                                                          "message": {"parts": [{"text": "over TLS"}]}}}}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "leaf.pem", tmp_path / "leaf.key")
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    agent = Agent.model_validate({"id": "alpha", "type": "hermes", "host": "h1",
                                  "reach": {"endpoint": f"https://localhost:{server.server_port}"}})
    yield agent, tmp_path / "ca.pem"
    server.shutdown()


def test_beacon_reaches_an_agent_over_tls_with_the_operators_ca(tls_agent, monkeypatch):
    agent, ca_file = tls_agent
    monkeypatch.setenv(mesh.SECRET_ENV, "test-mesh-secret")
    monkeypatch.delenv(bus.CA_FILE_ENV, raising=False)
    with pytest.raises(bus.Unreachable, match="CERTIFICATE_VERIFY_FAILED"):
        bus.call(agent, "hello")

    monkeypatch.setenv(bus.CA_FILE_ENV, str(ca_file))
    assert bus.call(agent, "hello")["reply"] == "over TLS"
