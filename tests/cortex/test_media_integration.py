import asyncio,time
import pytest,httpx
from engram.cortex.media import FrameHub,LocalJPEG
from engram.cortex.integration import GuardedUpstream
from engram.cortex.console_adapter import AsyncCortexMemory
from engram.cortex.api import create_app
from engram.cortex.types import CortexError,Denied
from engram.cortex.simulation import Result
TOKEN='synthetic-console-token-32-characters'

def test_latest_frame_buffer_is_one_and_tracks_capture_separately():
    h=FrameHub(lambda:None)
    for n in range(100):h.publish(b'\xff\xd8'+str(n).encode()+b'\xff\xd9')
    assert h.latest().seq==100 and h.metadata()['buffered_frames']==1
    assert h.metadata()['capture_age_s'] is None
    assert h.latest(after=100) is None

def test_stale_capture_is_not_relabelled_as_live():
    h=FrameHub(lambda:None);h.publish(b'\xff\xd8data\xff\xd9',captured_at=time.time()-30)
    m=h.metadata();assert m['capture_age_s']>=30 and m['receipt_age_s']<1

@pytest.mark.parametrize('url',['https://example.com/frame.jpg','http://127.0.0.1:8200/robot/execute','http://127.0.0.1:8200/robot/frame?key=x'])
def test_frame_source_is_fixed_read_only_loopback(url):
    with pytest.raises(CortexError):LocalJPEG(url)

def test_oversized_or_non_jpeg_refused():
    h=FrameHub(lambda:None,max_bytes=10)
    with pytest.raises(CortexError):h.publish(b'no')
    with pytest.raises(CortexError):h.publish(b'\xff\xd8'+b'0'*20+b'\xff\xd9')

def test_unverified_upstream_status_not_success(store):
    class U:
        def execute(self,op,args):return Result('done','not sensor checked; UNKNOWN E_UNVERIFIED')
    g=GuardedUpstream(U(),store)
    assert g.execute('gripper_open',{}).status=='unknown'

def test_location_reference_preserves_timestamp_not_fake_refresh(store,clock):
    store.observe('sample_a','bench',origin='camera',mode='PHYSICAL')
    original=clock();clock.advance(10)
    g=GuardedUpstream(None,store);r=g.execute('locate_tag',{'object_id':'sample_a'})
    assert r.location_evidence['sample_a']['captured_at']==original
    assert not r.observations
    assert next(o for o in store.objects() if o['id']=='sample_a')['age_s']==10

def test_simulated_location_not_physical_evidence(store):
    store.observe('sample_a','bench',origin='mock',mode='SIMULATED')
    assert GuardedUpstream(None,store).execute('locate_tag',{'object_id':'sample_a'}).status=='failed'

def test_console_memory_adapter_uses_canonical_api_and_no_identity_spoof(brain,monkeypatch):
    monkeypatch.setenv('CORTEX_API_TOKEN',TOKEN)
    app=create_app(brain,tokens={TOKEN:'carther'},start_idle=False)
    async def check():
        m=AsyncCortexMemory('http://127.0.0.1:8010',transport=httpx.ASGITransport(app=app))
        r=await m.add_rule('Safety goggles must be at the bench before handling Sample B.')
        q=await m.query('Bring Sample B to bench')
        assert q['facts'][0]['required_zone']=='bench'
        assert (await m.evidence(r['chunk_id']))['source_id']==r['source_id']
        with pytest.raises(Denied):await m.record_episode({'outcome':'success'})
        await m.close()
    asyncio.run(check())
