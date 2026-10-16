import ctypes.util
import hashlib
import json
import os
import sys
import numpy as np
import pytest
from oandafly.neural.artifacts import load_graph
from oandafly.neural.runtime import FullCNSModel
from oandafly.neural.isolation import InferenceProcess
from oandafly.neural.window import feature_window, infer_window
from oandafly.marketdata.history import CandleArchive
from oandafly.training.trainer import TrainingSpec, CostAssumptions, train_candidate, net_utility_label, read_candles
from oandafly.domain.primitives import sha256_json
from decimal import Decimal as D

def costs():
    return CostAssumptions(evidence_sha256='c'*64,commission_round_trip_fraction=D('.00001'),
        slippage_per_side_fraction=D('.000005'),financing_long_per_day_fraction=D('.0001'),
        financing_short_per_day_fraction=D('.0001'))

def test_all_declared_nodes_and_internal_edges_are_retained(graph_model):
    root,manifest,_=graph_model
    graph,arrays=load_graph(root/'graph')
    assert graph.neurons==3 and graph.connections==3 and graph.excluded_endpoint_rows==1
    assert graph.scope=='TEST_FIXTURE'
    assert arrays['indptr.npy'].shape==(4,)

def test_fixture_graph_cannot_enter_production_inference(graph_model):
    root,_,_=graph_model
    with pytest.raises(Exception): FullCNSModel(root/'graph',root/'model')

def test_full_state_and_weights_are_deterministic(graph_model):
    root,_,_=graph_model
    one=FullCNSModel(root/'graph',root/'model',allow_fixture=True)
    two=FullCNSModel(root/'graph',root/'model',allow_fixture=True)
    for _ in range(4):
        assert one.infer(np.ones(12,dtype=np.float32))==two.infer(np.ones(12,dtype=np.float32))
    assert one.kernel.state.shape==(3,) and np.isfinite(one.kernel.state).all()
    assert one.observations==4

def test_artifact_byte_tampering_is_rejected(graph_model):
    root,_,_=graph_model
    p=next((root/'model').glob('*.npy'))
    content=bytearray(p.read_bytes());content[-1]^=1;p.write_bytes(content)
    with pytest.raises(Exception): FullCNSModel(root/'graph',root/'model',allow_fixture=True)

@pytest.mark.contained
@pytest.mark.linux_containment
@pytest.mark.skipif(sys.platform!='linux' or not ctypes.util.find_library('seccomp'),reason='Linux libseccomp required')
async def test_contained_direct_scope_and_restart_window_match(graph_model,candles):
    root,_,_=graph_model
    process=InferenceProcess(graph=root/'graph',model=root/'model',instruments=('EUR_USD','GBP_USD'),allow_fixture=True)
    try:
        assert process.contained
        direct=FullCNSModel(root/'graph',root/'model',allow_fixture=True)
        values=np.ones(12,dtype=np.float32)
        assert process.infer('EUR_USD',values).utilities==direct.infer(values)
        assert process.infer('EUR_USD',values).observations==2
        assert process.infer('GBP_USD',values).observations==1
        window=feature_window(candles,2)
        first=await infer_window(process,'EUR_USD',window,deadline_seconds=10)
        second=await infer_window(process,'EUR_USD',window,deadline_seconds=10)
        assert first==second and first.observations==2
        with pytest.raises(Exception): process.infer('USD_JPY',values)
    finally: process.close()

def test_training_uses_next_bar_and_explicit_friction(candles):
    result=net_utility_label(candles,35,3,costs())
    assert result is not None
    assert result[0] < float((candles[38].bid_close-candles[36].ask_open)/candles[36].ask_open)
    assert net_utility_label(candles,len(candles)-1,3,costs()) is None

def test_real_candidate_cannot_be_trained_from_a_fixture_by_default(graph_model,candles):
    root,_,_=graph_model;CandleArchive(root/'archive').write(candles)
    with pytest.raises(ValueError,match='complete declared graph'):
        train_candidate(graph_directory=root/'graph',archive_directory=root/'archive',instrument='EUR_USD',
            destination=root/'candidate',spec=TrainingSpec(),costs=costs())

def test_causal_candidate_trains_publishes_and_replays_without_execution_authority(graph_model,candles):
    root,_,_=graph_model;CandleArchive(root/'archive').write(candles)
    spec=TrainingSpec(kernel={'neural_warmup_bars':2},maximum_seconds=30,maximum_bars=320)
    result=train_candidate(graph_directory=root/'graph',archive_directory=root/'archive',instrument='EUR_USD',
        destination=root/'candidate',spec=spec,costs=costs(),allow_fixture=True)
    assert result['status']=='UNQUALIFIED_CANDIDATE'
    assert set(result['metrics'])=={'train','validation','forward_research'}
    assert min(m['observations'] for m in result['metrics'].values())>=5
    model=FullCNSModel(root/'graph',root/'candidate',allow_fixture=True)
    assert np.isfinite(model.infer(np.ones(12,dtype=np.float32))).all()
    receipt=json.loads((root/'candidate'/'training-receipt.json').read_text())
    assert model.manifest.training_receipt_sha256==sha256_json(receipt)

def test_archive_write_survives_crashed_temp_and_concurrent_publish(tmp_path,candles,monkeypatch):
    archive=CandleArchive(tmp_path/'archive');rows=candles[:50]
    data=b"\n".join(c.model_dump_json().encode('utf-8') for c in rows)+b"\n"
    crashed=tmp_path/'archive'/('.'+hashlib.sha256(data).hexdigest()+'.tmp');crashed.write_bytes(b'left by a killed writer')
    real_link=os.link
    def racing_link(source,destination):
        real_link(source,destination);raise FileExistsError(destination)  # another writer published first
    monkeypatch.setattr(os,'link',racing_link)
    path=archive.write(rows)
    assert path.read_bytes()==data and sorted((tmp_path/'archive').glob('.*.tmp'))==[crashed]

def test_archive_rolling_selection_is_exact_and_bounded(tmp_path,candles):
    archive=CandleArchive(tmp_path/'archive');archive.write(candles[:200]);archive.write(candles[180:])
    selected,_=read_candles(tmp_path/'archive',instrument='EUR_USD',maximum_bars=100)
    assert selected==candles[-100:]
