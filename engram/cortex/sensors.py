"""Narrow trusted sensor-ingest boundary. Never infer a zone from a bare tag.

The configured sensor process must supply calibrated object/zone association,
original capture time, boot/sequence and frame/calibration references. Authenticity
comes from a distinct service credential, not the JSON origin string.
"""
from .types import Conflict,Denied,canonical,digest,finite,text


def ingest(store, packet:dict, *, sensor_principal:str, runtime_mode:str)->dict:
    if not sensor_principal.startswith('sensor:'): raise Denied('sensor principal required')
    required={'object_id','zone','captured_at','boot_id','sequence','frame_id','calibration_id'}
    if set(packet)!=required: raise Conflict('sensor packet fields do not match contract')
    for k in required-{'sequence','captured_at'}:text(packet[k],k,160)
    sequence=packet['sequence']
    if isinstance(sequence,bool) or not isinstance(sequence,int) or not 0<=sequence<2**53:raise Conflict('invalid sensor sequence')
    finite(packet['captured_at'],'capture UTC',0,store.clock()+1)
    payload_hash=digest(packet)
    with store.tx():
        store.conn.execute('CREATE TABLE IF NOT EXISTS cx_sensor_packet(principal TEXT,boot_id TEXT,sequence INTEGER,input_hash TEXT,observation_id TEXT NOT NULL,PRIMARY KEY(principal,boot_id,sequence))')
        old=store.one('SELECT * FROM cx_sensor_packet WHERE principal=? AND boot_id=? AND sequence=?',(sensor_principal,packet['boot_id'],sequence))
        if old:
            if old['input_hash']!=payload_hash:raise Conflict('sensor sequence reused with changed data')
            return {'observation_id':old['observation_id'],'duplicate':True}
        # Retain old captures as old, never give receipt time to a delayed frame.
        evidence=canonical({'frame_id':packet['frame_id'],'calibration_id':packet['calibration_id'],
                            'boot_id':packet['boot_id'],'sequence':sequence})
        oid=store.observe(packet['object_id'],packet['zone'],origin=sensor_principal,
             mode=runtime_mode,captured_at=packet['captured_at'],evidence=evidence)
        store.conn.execute('INSERT INTO cx_sensor_packet VALUES(?,?,?,?,?)',
             (sensor_principal,packet['boot_id'],sequence,payload_hash,oid))
    return {'observation_id':oid,'duplicate':False,'evidence_class':runtime_mode}
