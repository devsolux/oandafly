"""Explicit tiny graph fixtures. Never full MaleCNS or broker qualification."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import numpy as np
import pytest
from oandafly.marketdata.contracts import Candle
from oandafly.neural.importer import compile_graph
from oandafly.neural.artifacts import KernelConfig
from oandafly.neural.publishing import publish_model

@pytest.fixture
def graph_model(tmp_path):
    (tmp_path/'neurons.csv').write_text('bodyId\n1\n2\n3\n')
    (tmp_path/'signs.csv').write_text('bodyId,sign\n1,1\n2,-1\n3,1\n')
    (tmp_path/'edges.csv').write_text('bodyId_pre,bodyId_post,weight\n1,2,5\n2,3,8\n3,1,1\n9,1,3\n')
    graph=compile_graph(neurons_path=tmp_path/'neurons.csv', connections_path=tmp_path/'edges.csv',
        signs_path=tmp_path/'signs.csv',destination=tmp_path/'graph',release='TEST_ONLY',scope='TEST_FIXTURE')
    model=publish_model(tmp_path/'model',graph=graph,encoder=np.ones((3,12),dtype=np.float32)*.01,
        readout=np.array([[.1,.2,.3],[-.2,-.1,-.3]],dtype=np.float32),bias=np.zeros(2,dtype=np.float32),
        kernel=KernelConfig(neural_warmup_bars=2),training_receipt_sha256='a'*64,
        seed=1,confidence_scale=.01,allocation_scale=.01)
    return tmp_path,graph,model

@pytest.fixture
def candles():
    start=datetime(2025,1,6,tzinfo=timezone.utc)
    result=[]
    for i in range(320):
        opening=Decimal('1.10000')+Decimal(i)*Decimal('.00001')
        high,low,closing=opening+Decimal('.00004'),opening-Decimal('.00002'),opening+Decimal('.00001')
        spread=Decimal('.00002')
        result.append(Candle(instrument='EUR_USD',start=start+timedelta(minutes=5*i),
            end=start+timedelta(minutes=5*(i+1)),bid_open=opening,bid_high=high,bid_low=low,bid_close=closing,
            ask_open=opening+spread,ask_high=high+spread,ask_low=low+spread,ask_close=closing+spread,
            volume=100+i%8,quantum=Decimal('.00001'),source='PAPER'))
    return tuple(result)
