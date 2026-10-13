from datetime import timedelta
from decimal import Decimal as D
import sqlite3
import pytest
from pydantic import ValidationError
from oandafly.domain.capabilities import RuntimeMode, AuthorizationGrant, RiskLimits
from oandafly.domain.decisions import Action
from oandafly.domain.errors import SafetyFence, ValidationFailure
from oandafly.domain.primitives import utc_now
from oandafly.marketdata.contracts import Quote, MarketCut
from oandafly.neural.window import feature_window
from oandafly.neural.policy import decision
from oandafly.portfolio.planner import OpenTrade, residual_plan
from oandafly.execution.risk import AccountExposureRisk, require_account_capacity

@pytest.mark.parametrize('mode,environment,allowed', [
    ('PRACTICE','SANDBOX',True),('LIVE','LIVE',True),('SHADOW','LIVE',False),
    ('SHADOW','SANDBOX',False),('PAPER','SANDBOX',False),('REPLAY','SANDBOX',False)])
def test_explicit_native_mutation_capabilities(mode,environment,allowed):
    m=RuntimeMode(mode);m.require_environment(environment)
    assert m.broker_mutation is allowed

@pytest.mark.parametrize('mode,environment', [('LIVE','SANDBOX'),('PRACTICE','LIVE'),('PAPER','LIVE'),('REPLAY','LIVE'),('SHADOW','FAKE')])
def test_mode_environment_mismatch_is_denied(mode,environment):
    with pytest.raises(SafetyFence):RuntimeMode(mode).require_environment(environment)

def test_live_grant_never_authorizes_shadow_or_another_account():
    now=utc_now();grant=AuthorizationGrant(grant_id='g',authority_id='a',environment='LIVE',mode='LIVE',
        account_id='001-001-1234567-001',profile_sha256='a'*64,instruments=('EUR_USD',),limits=RiskLimits(),
        issued_at=now-timedelta(minutes=1),expires_at=now+timedelta(minutes=1),recovery_until=now+timedelta(minutes=2))
    kwargs=dict(environment='LIVE',account_id=grant.account_id,authority_id='a',profile_sha256='a'*64,action='ENTRY',instrument='EUR_USD')
    grant.require(mode=RuntimeMode.LIVE,**kwargs)
    with pytest.raises(SafetyFence):grant.require(mode=RuntimeMode.SHADOW,**kwargs)
    with pytest.raises(SafetyFence):grant.require(mode=RuntimeMode.LIVE,**{**kwargs,'account_id':'other'})
    with pytest.raises(SafetyFence):grant.require(mode=RuntimeMode.LIVE,**{**kwargs,'instrument':'GBP_USD'})
    with pytest.raises(SafetyFence):grant.require(mode=RuntimeMode.LIVE,now=now+timedelta(seconds=90),**kwargs)
    grant.require(mode=RuntimeMode.LIVE,now=now+timedelta(seconds=90),**{**kwargs,'action':'RECOVERY'})

def test_half_tick_midpoint_is_exact_without_weakening_quote_lattice():
    now=utc_now();q=Quote(instrument='EUR_USD',bid=D('1.10000'),ask=D('1.10001'),quantum=D('.00001'),
        observed_at=now,received_at=now,tradeable=True,source='PAPER')
    assert q.midpoint==D('1.100005') and q.midpoint_quantum==D('.000005')
    with pytest.raises(ValidationError):Quote.model_validate({**q.model_dump(),'bid':D('1.100001')})
    with pytest.raises(SafetyFence):q.require_fresh(now+timedelta(seconds=6),5)

def test_warmup_and_gaps_are_explicit(candles):
    assert feature_window(candles[:4],2).state=='WARMUP'
    assert feature_window(candles[:33],2).required==34
    assert feature_window(candles[:34],2).state=='READY'
    assert not feature_window(candles[:20]+candles[21:35],2).frames

def test_cut_refuses_environment_mix(candles):
    bar=candles[-1];now=bar.end
    quote=Quote(instrument=bar.instrument,bid=bar.bid_close,ask=bar.ask_close,quantum=bar.quantum,
        observed_at=now,received_at=now,tradeable=True,source='OANDA_LIVE')
    with pytest.raises(ValidationError,match='mixes environments'):
        MarketCut(instrument=bar.instrument,as_of=now,quote=quote,candles=candles,
            account_frontier='0',account_sha256='a'*64)

@pytest.mark.parametrize('target,held,operation,units', [
    ('100','100','NOOP','0'),('100','60','ENTRY','40'),('-100','60','CLOSE_TRADE','60'),
    ('0','60','CLOSE_TRADE','60'),('30','60','CLOSE_TRADE','30'),('-80','-40','ENTRY','-40')])
def test_repeated_targets_and_reversals_do_not_multiply_exposure(target,held,operation,units):
    step=residual_plan(D(target),(OpenTrade('10','EUR_USD',D(held)),))
    assert step.operation==operation and step.units==D(units)

def test_unknown_obligations_always_block_residual_entry():
    assert residual_plan(D('100'),(),pending=True).operation=='WAIT'

def test_gross_and_currency_caps_include_pending_reservations():
    exposure=AccountExposureRisk(equity=D('1000'),current_gross_notional=D('500'),
        currency_gross_notional={'EUR':D('500'),'USD':D('500')},maximum_gross_leverage=D('2'),maximum_currency_fraction=D('1'))
    require_account_capacity(exposure,base='EUR',quote='USD',new_notional=D('400'),other_reserved_margin=D('0'))
    with pytest.raises(SafetyFence):require_account_capacity(exposure,base='EUR',quote='USD',new_notional=D('400'),other_reserved_margin=D('100'))

def test_financial_risk_limits_reject_binary_floats():
    with pytest.raises((ValidationError,ValueError,TypeError)):RiskLimits(maximum_entry_loss_fraction=.0025)

def test_utility_decoder_has_no_indicator_direction_fallback(graph_model,candles):
    _,_,model=graph_model;bar=candles[-1];now=bar.end
    quote=Quote(instrument=bar.instrument,bid=bar.bid_close,ask=bar.ask_close,quantum=bar.quantum,
        observed_at=now,received_at=now,tradeable=True,source='PAPER')
    cut=MarketCut(instrument=bar.instrument,as_of=now,quote=quote,candles=candles,account_frontier='0',account_sha256='a'*64)
    long=decision(cut,model,utilities=(.2,.1),observations=2,warmup_required=2)
    short=decision(cut,model,utilities=(.1,.2),observations=2,warmup_required=2)
    missing=decision(cut,model,utilities=None,observations=0,warmup_required=2)
    flat=decision(cut,model,utilities=(-.1,-.2),observations=2,warmup_required=2)
    assert long.preference>0 and short.preference<0 and missing.action==Action.ABSTAIN and flat.action==Action.FLATTEN

def test_actual_old_sqlite_runtime_is_rejected_without_bypass(tmp_path):
    from oandafly.storage.database import Database
    if sqlite3.sqlite_version_info >= (3,51,3):
        db=Database(tmp_path/'valid.db');db.close()
    else:
        with pytest.raises(ValidationFailure,match='3.51.3'):Database(tmp_path/'denied.db')
