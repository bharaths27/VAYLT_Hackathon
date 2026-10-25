import importlib.util,json,sqlite3,subprocess,sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
def load_script(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'scripts'/f'{name}.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m

def test_complete_demo_and_separate_process_restart(tmp_path):
    output=tmp_path/'demo.json'
    subprocess.run([sys.executable,str(ROOT/'scripts/cortex_demo.py'),'--out',str(output)],cwd=ROOT,check=True,capture_output=True,timeout=15)
    d=json.loads(output.read_text());assert d['all_passed'] and len(d['checks'])==7
    assert d['mode']=='SIMULATED' and not d['actual_model_inference']

def test_sqlite_backup_preserves_wal_and_refuses_overwrite(tmp_path):
    mod=load_script('cortex_backup');source=tmp_path/'source.db';dest=tmp_path/'backup.db'
    conn=sqlite3.connect(source);conn.execute('PRAGMA journal_mode=WAL');conn.execute('CREATE TABLE notes(n TEXT)');conn.execute("INSERT INTO notes VALUES('persisted')");conn.commit()
    mod.backup(source,dest)
    with sqlite3.connect(dest) as c:assert c.execute('SELECT n FROM notes').fetchone()[0]=='persisted'
    with pytest.raises(FileExistsError):mod.backup(source,dest)
    with pytest.raises(ValueError):mod.backup(source,source)
    conn.close()

def test_opt_in_console_patch_rejects_changed_anchors_and_preserves_voice(tmp_path):
    mod=load_script('cortex_console_patch');root=tmp_path
    (root/'console/server/adapters').mkdir(parents=True)
    for f in ('brain.py','adapters/memory.py'):(root/'console/server'/f).write_text((ROOT/'console/server'/f).read_text())
    changed=mod.transforms(root)
    assert len(changed)==2 and all('voice' not in x[0] for x in changed)
    for path,old,new in changed:(root/path).write_text(new)
    again=mod.transforms(root);assert all(old==new for _,old,new in again)
    (root/'console/server/brain.py').write_text('unknown upstream version')
    with pytest.raises(ValueError):mod.transforms(root)
