from datetime import timedelta
import base64
import json
import os
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from oandafly.security.attestations import (TrustedSigner,TrustPolicy,SignedDocument,ApprovalVerifier,write_private_json,private_document)
from oandafly.domain.errors import SafetyFence
from oandafly.domain.primitives import utc_now

def make_docs(tmp_path):
    now=utc_now();key=Ed25519PrivateKey.generate()
    signer=TrustedSigner(key_id='grant-test',role='TRADING_AUTHORIZATION',
        public_key_hex=key.public_key().public_bytes(serialization.Encoding.Raw,serialization.PublicFormat.Raw).hex(),
        valid_from=now-timedelta(minutes=1),valid_until=now+timedelta(hours=1))
    trust=TrustPolicy(keys=(signer,))
    draft=SignedDocument(key_id='grant-test',role='TRADING_AUTHORIZATION',payload={'claim':'TEST_ONLY'},signature_b64=base64.b64encode(bytes(64)).decode())
    signed=SignedDocument.from_wire({**draft.model_dump(mode='json'),'signature_b64':base64.b64encode(key.sign(draft.signing_bytes())).decode()})
    write_private_json(tmp_path/'trust.json',trust);write_private_json(tmp_path/'approval.json',signed)
    return trust,signed,ApprovalVerifier(tmp_path/'trust.json')

def test_signature_role_and_live_revocation(tmp_path):
    trust,signed,verifier=make_docs(tmp_path)
    assert verifier.verify(tmp_path/'approval.json',role='TRADING_AUTHORIZATION').digest==signed.digest
    with pytest.raises(SafetyFence):verifier.verify(tmp_path/'approval.json',role='DEPLOYMENT')
    revoked=TrustPolicy(keys=trust.keys,revoked_document_sha256=(signed.digest,))
    write_private_json(tmp_path/'trust.json',revoked,replace=True)
    with pytest.raises(SafetyFence,match='revoked'):verifier.verify(tmp_path/'approval.json',role='TRADING_AUTHORIZATION')

def test_unsigned_payload_change_is_rejected(tmp_path):
    _,signed,verifier=make_docs(tmp_path)
    tampered=signed.model_dump(mode='json');tampered['payload']['claim']='READY'
    write_private_json(tmp_path/'approval.json',tampered,replace=True)
    with pytest.raises(SafetyFence):verifier.verify(tmp_path/'approval.json',role='TRADING_AUTHORIZATION')

def test_signer_roles_must_use_different_keys(tmp_path):
    trust,_,_=make_docs(tmp_path);key=trust.keys[0]
    other=TrustedSigner.from_wire({**key.model_dump(mode='json'),'key_id':'other','role':'MODEL_VALIDATION'})
    with pytest.raises(ValueError):TrustPolicy(keys=(key,other))

@pytest.mark.posix_only
@pytest.mark.skipif(os.name=='nt',reason='POSIX ownership semantics')
def test_owner_only_approval_and_symlinks_enforced(tmp_path):
    path=tmp_path/'claim.json';write_private_json(path,{'test':True});assert private_document(path)
    path.chmod(0o644)
    with pytest.raises(SafetyFence):private_document(path)
    path.chmod(0o600);alias=tmp_path/'alias.json';alias.symlink_to(path)
    with pytest.raises(SafetyFence):private_document(alias)

def test_approval_never_overwrites_without_explicit_rotation(tmp_path):
    path=tmp_path/'claim.json';write_private_json(path,{'test':True})
    with pytest.raises(FileExistsError):write_private_json(path,{'test':False})
    assert json.loads(private_document(path))=={'test':True}
