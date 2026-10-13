"""Fixed-origin broker contract mocks, not native account qualification."""
from datetime import timedelta
from decimal import Decimal as D
from uuid import uuid4
import httpx
import pytest
from pydantic import SecretStr
from oandafly.adapters.governor import CredentialProbeGovernor
from oandafly.adapters.oanda.execution import NativeExecutionAdapter
from oandafly.adapters.oanda.observations import NativeReadAdapter, quote_from_native
from oandafly.domain.capabilities import RuntimeMode
from oandafly.domain.errors import SafetyFence, TransportAmbiguous
from oandafly.domain.instruments import CurrencyInstrument
from oandafly.domain.primitives import utc_now
from oandafly.security.credentials import CredentialReference

ACCOUNT='001-001-1234567-001'
class Vault:
    def load(self, reference):return SecretStr('SYNTHETIC_TEST_ONLY_NEVER_OANDA')
def ref(environment='SANDBOX',purpose='V20_EXECUTION'):
    return CredentialReference(credential_id=uuid4(),authority_id='test',environment=environment,purpose=purpose,
        account_ids=(ACCOUNT,),expires_at=utc_now()+timedelta(hours=1))
def adapter(handler,environment='SANDBOX',mode=RuntimeMode.PRACTICE,authorizer=lambda account,action:None):
    return NativeExecutionAdapter(environment=environment,mode=mode,credential=ref(environment),vault=Vault(),
        governor=CredentialProbeGovernor(),authorize=authorizer,transport=httpx.MockTransport(handler))
def document():return {'orderCreateTransaction':{'id':'10','type':'MARKET_ORDER','accountID':ACCOUNT},'lastTransactionID':'10'}

@pytest.mark.parametrize('environment,mode,host',[('SANDBOX',RuntimeMode.PRACTICE,'api-fxpractice.oanda.com'),('LIVE',RuntimeMode.LIVE,'api-fxtrade.oanda.com')])
async def test_exact_entry_bytes_fixed_host_and_one_shot_preparation(environment,mode,host):
    requests=[]
    def handler(request):requests.append(request);return httpx.Response(201,json=document())
    instance=adapter(handler,environment,mode)
    body=b'{"order":{"instrument":"EUR_USD","units":"100","positionFill":"OPEN_ONLY","type":"MARKET"}}'
    try:
        await instance.prepare_order(ACCOUNT);await instance.send_order_bytes(ACCOUNT,body)
        with pytest.raises(SafetyFence):await instance.send_order_bytes(ACCOUNT,body)
        assert len(requests)==1 and requests[0].url.host==host and requests[0].content==body
    finally:await instance.close()

@pytest.mark.parametrize('status',[301,401,429,500,503])
async def test_ambiguous_http_completion_never_retries(status):
    requests=[]
    def handler(request):requests.append(request);return httpx.Response(status,json={'errorMessage':'PRIVATE'})
    instance=adapter(handler)
    try:
        await instance.prepare_order(ACCOUNT)
        with pytest.raises(TransportAmbiguous) as error:await instance.send_order_bytes(ACCOUNT,b'{}')
        assert len(requests)==1 and 'PRIVATE' not in str(error.value)
    finally:await instance.close()

async def test_exact_native_trade_close_is_not_an_opposite_entry():
    calls=[];guard=[]
    def handler(request):
        assert guard==['guard'];calls.append(request);return httpx.Response(200,json=document())
    instance=adapter(handler)
    try:
        await instance.close_trade(ACCOUNT,'123',D('4'),before_send=lambda:guard.append('guard'))
        assert calls[0].method=='PUT' and calls[0].url.path.endswith('/trades/123/close')
        assert calls[0].content==b'{"units":"4"}'
    finally:await instance.close()

async def test_failed_final_recovery_guard_prevents_network_write():
    instance=adapter(lambda req:pytest.fail('No transport invocation is allowed'))
    def deny():raise SafetyFence('revoked')
    try:
        with pytest.raises(SafetyFence):await instance.close_trade(ACCOUNT,'123',D('4'),before_send=deny)
    finally:await instance.close()

def test_shadow_cannot_construct_any_mutation_port():
    with pytest.raises(SafetyFence):adapter(lambda req:None,mode=RuntimeMode.SHADOW)

def test_price_parser_preserves_native_half_tick_midpoint():
    now=utc_now();row={'instrument':'EUR_USD','time':now.isoformat(),'status':'tradeable',
        'bids':[{'price':'1.10000','liquidity':1000000}],'asks':[{'price':'1.10001','liquidity':1000000}]}
    q=quote_from_native(row,instrument=CurrencyInstrument(name='EUR_USD'),environment='SANDBOX',received_at=now)
    assert q.midpoint==D('1.100005')

async def test_native_candles_filter_incomplete_and_request_bid_ask_only(candles):
    bars=candles[:2];requests=[]
    def row(c,complete):return {'time':c.start.isoformat(),'complete':complete,'volume':c.volume,
        'bid':dict(zip('ohlc',map(str,(c.bid_open,c.bid_high,c.bid_low,c.bid_close)))),
        'ask':dict(zip('ohlc',map(str,(c.ask_open,c.ask_high,c.ask_low,c.ask_close))))}
    def handler(request):
        requests.append(request);return httpx.Response(200,json={'instrument':'EUR_USD','granularity':'M5',
            'candles':[row(bars[0],True),row(bars[1],False)]})
    instance=NativeReadAdapter(environment='SANDBOX',credential=ref(purpose='V20_OBSERVATION'),vault=Vault(),
        governor=CredentialProbeGovernor(),transport=httpx.MockTransport(handler))
    try:
        result=await instance.candles(ACCOUNT,CurrencyInstrument(name='EUR_USD'),count=2)
        assert len(result)==1 and result[0].source=='OANDA_SANDBOX'
        assert requests[0].url.params['price']=='BA'
    finally:await instance.close()
