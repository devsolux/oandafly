"""Isolated in-memory SQL algorithms, NOT production SQLite/WAL/lock qualification.

The production Database is never constructed or patched here. SQL routines are
also exercised against the supported production store in the integration suite.
"""
from contextlib import contextmanager
from datetime import timedelta
from decimal import Decimal as D
from itertools import permutations
import sqlite3
from types import SimpleNamespace
import pytest
from oandafly.domain.capabilities import RiskLimits
from oandafly.domain.decisions import PolicyDecision, Action
from oandafly.domain.errors import SafetyFence, Conflict
from oandafly.domain.primitives import utc_now
from oandafly.storage.ledger import Ledger
from oandafly.execution.outbox import Outbox
from oandafly.execution.reconciler import OutboxReducer
from oandafly.portfolio.planner import TargetStore
from oandafly.portfolio.equity import EquityLimits

pytestmark=pytest.mark.sqlite_algorithm
ACCOUNT='001-001-1234567-001'

class AlgorithmStore:
    """Test-only SQL state machine host; not an application Database replacement."""
    def __init__(self):
        self.connection=sqlite3.connect(':memory:',isolation_level=None)
        self.connection.row_factory=sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
    def initialize(self,sql,**kwargs):self.connection.executescript(sql)
    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield self.connection
            self.connection.commit()
        except BaseException:
            self.connection.rollback();raise
    @contextmanager
    def read(self):yield self.connection
    def close(self):self.connection.close()

@pytest.fixture
def system():
    db=AlgorithmStore();ledger=Ledger(db)
    db.initialize(Outbox.SCHEMA)
    outbox=SimpleNamespace(db=db)
    reducer=OutboxReducer(outbox,ledger=ledger)
    ledger.establish_baseline('SANDBOX',ACCOUNT,'USD',D('1000'),'0',utc_now())
    reducer.seed_snapshot(environment='SANDBOX',account_id=ACCOUNT,cursor='0')
    try:yield db,ledger,reducer
    finally:db.close()

def fill(identity,**fields):
    return {'id':str(identity),'accountID':ACCOUNT,'type':'ORDER_FILL','orderID':str(identity-1),
        'instrument':'EUR_USD','time':'2025-01-06T00:00:00Z','pl':'0','financing':'0',
        'commission':'0','accountBalance':'1000',**fields}

FACTS=(fill(10,units='10',tradeOpened={'tradeID':'10','units':'10'}),
       fill(12,units='-6',tradeReduced={'tradeID':'10','units':'6'}),
       fill(14,units='-4',tradesClosed=[{'tradeID':'10','units':'4'}]))

@pytest.mark.parametrize('order',tuple(permutations(range(3))))
def test_observation_permutations_replay_to_same_flat_account(system,order):
    db,ledger,reducer=system
    for index in order:
        fact=FACTS[index]
        assert not reducer.ingest(fact,environment='SANDBOX',account_id=ACCOUNT).quarantined
        assert reducer.ingest(fact,environment='SANDBOX',account_id=ACCOUNT).duplicate
    # Uncovered observations alone never create authoritative position state.
    assert db.connection.execute('SELECT COUNT(*) FROM execution_trades').fetchone()[0]==0
    assert reducer.apply_history(environment='SANDBOX',account_id=ACCOUNT,requested_from_id='0',
        transactions=[FACTS[i] for i in reversed(order)],last_transaction_id='14')
    trade=db.connection.execute('SELECT current_units,last_transaction_id FROM execution_trades').fetchone()
    assert tuple(trade)==('0','14')
    assert reducer.require_caught_up(environment='SANDBOX',account_id=ACCOUNT)=='14'
    assert ledger.balance('SANDBOX',ACCOUNT).balance==D('1000')

def test_partial_reduction_frontier_never_moves_backward(system):
    db,_,reducer=system
    facts=(FACTS[0],FACTS[1],fill(14,units='-2',tradeReduced={'tradeID':'10','units':'2'}))
    for fact in reversed(facts):reducer.ingest(fact,environment='SANDBOX',account_id=ACCOUNT)
    assert reducer.apply_history(environment='SANDBOX',account_id=ACCOUNT,requested_from_id='0',
        transactions=facts,last_transaction_id='14')
    assert tuple(db.connection.execute('SELECT current_units,last_transaction_id FROM execution_trades').fetchone())==('2','14')

def test_same_native_id_conflict_remains_durably_fenced(system):
    db,_,reducer=system
    reducer.ingest(FACTS[0],environment='SANDBOX',account_id=ACCOUNT)
    conflict={**FACTS[0],'units':'11'}
    assert reducer.ingest(conflict,environment='SANDBOX',account_id=ACCOUNT).quarantined
    assert db.connection.execute('SELECT COUNT(*) FROM execution_fences').fetchone()[0]>0
    with pytest.raises(SafetyFence):reducer.require_caught_up(environment='SANDBOX',account_id=ACCOUNT)

def test_baseline_is_atomic_and_cannot_be_rebound():
    db=AlgorithmStore();ledger=Ledger(db);db.initialize(Outbox.SCHEMA);reducer=OutboxReducer(SimpleNamespace(db=db),ledger=ledger)
    try:
        with pytest.raises(ValueError):
            with db.transaction() as conn:
                ledger.establish_baseline_in(conn,'SANDBOX',ACCOUNT,'USD',D('1000'),'8',utc_now())
                raise ValueError('failure between the cash and position baseline')
        assert db.connection.execute('SELECT COUNT(*) FROM ledger_accounts').fetchone()[0]==0
        with db.transaction() as conn:
            ledger.establish_baseline_in(conn,'SANDBOX',ACCOUNT,'USD',D('1000'),'8',utc_now())
            reducer.seed_snapshot_in(conn,environment='SANDBOX',account_id=ACCOUNT,cursor='8')
        with pytest.raises(Conflict):reducer.seed_snapshot(environment='SANDBOX',account_id=ACCOUNT,cursor='9')
    finally:db.close()

def policy(action=Action.SET_TARGET,preference=.5):
    now=utc_now()
    return PolicyDecision(decision_id='a'*64,model_sha256='b'*64,market_cut_sha256='c'*64,instrument='EUR_USD',
        observed_at=now,valid_until=now+timedelta(seconds=10),action=action,preference=preference,
        confidence=.9,reason='TEST_ONLY')

def test_decision_bar_is_consumed_once_and_cannot_reenter_after_restart(system):
    db,_,_=system;targets=TargetStore(db);p=policy();bar=p.observed_at-timedelta(seconds=1)
    args=dict(environment='SANDBOX',account_id=ACCOUNT,bar_end=bar,units=D('100'),frontier='0',target_expires_at=utc_now()+timedelta(minutes=5))
    assert targets.accept(p,**args).generation==1
    assert TargetStore(db).accept(p,**args) is None
    targets.suspend_all('SANDBOX',ACCOUNT,reason='RESTART')
    assert targets.current('SANDBOX',ACCOUNT,'EUR_USD') is None

def test_deposit_before_first_observation_does_not_loosen_drawdown_endowment(system):
    db,ledger,reducer=system;limits=EquityLimits(db);args=dict(environment='SANDBOX',account_id=ACCOUNT,source_sha256='c'*64,limits=RiskLimits())
    transfer={'id':'2','accountID':ACCOUNT,'type':'TRANSFER_FUNDS','amount':'500','accountBalance':'1500','time':'2025-01-06T00:00:00Z'}
    reducer.apply_history(environment='SANDBOX',account_id=ACCOUNT,requested_from_id='0',transactions=[transfer],last_transaction_id='2')
    status=limits.observe(nav=D('1500'),frontier='2',observed_at=utc_now(),**args)
    assert status.adjusted_nav==D('1000') and status.hard_fence is None
    assert D(db.connection.execute('SELECT initial_equity FROM portfolio_equity_limits').fetchone()[0])==D('1000')

def test_deposit_neither_earns_reward_nor_resets_latched_loss_fence(system):
    db,ledger,reducer=system;limits=EquityLimits(db);now=utc_now();args=dict(environment='SANDBOX',account_id=ACCOUNT,source_sha256='c'*64,limits=RiskLimits())
    assert limits.observe(nav=D('1000'),frontier='0',observed_at=now,**args).hard_fence is None
    transfer={'id':'2','accountID':ACCOUNT,'type':'TRANSFER_FUNDS','amount':'100','accountBalance':'1100','time':'2025-01-06T00:00:00Z'}
    reducer.apply_history(environment='SANDBOX',account_id=ACCOUNT,requested_from_id='0',transactions=[transfer],last_transaction_id='2')
    status=limits.observe(nav=D('1100'),frontier='2',observed_at=now+timedelta(seconds=1),**args)
    assert status.adjusted_nav==D('1000') and status.drawdown==0
    loss=limits.observe(nav=D('1080'),frontier='2',observed_at=now+timedelta(seconds=2),**args)
    assert loss.hard_fence=='DAILY_LOSS_LIMIT'
    gain=limits.observe(nav=D('1200'),frontier='2',observed_at=now+timedelta(seconds=3),**args)
    assert gain.hard_fence=='DAILY_LOSS_LIMIT'
