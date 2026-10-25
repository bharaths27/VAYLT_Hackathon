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

def test_a_destination_swapped_for_a_symlink_never_overwrites_another_database(tmp_path,monkeypatch):
    # Round 12: the old backup reserved the destination with O_EXCL, closed it and reopened it by name, so a symlink swapped
    # in between pointed SQLite at another database and the backup overwrote it.
    import os
    mod=load_script('cortex_backup');source,dest,victim=tmp_path/'source.db',tmp_path/'backup.db',tmp_path/'victim.db'
    with sqlite3.connect(source) as c:c.execute('CREATE TABLE notes(n TEXT)');c.execute("INSERT INTO notes VALUES('persisted')")
    with sqlite3.connect(victim) as c:c.execute('CREATE TABLE kept(v TEXT)');c.execute("INSERT INTO kept VALUES('untouched')")
    try:os.symlink(victim,tmp_path/'probe')
    except OSError:pytest.skip('this user cannot create symlinks here')
    def swap():                                                       # the destination becomes a symlink to another database
        if os.path.lexists(dest):os.unlink(dest)
        os.symlink(victim,dest)
    connect,link=sqlite3.connect,os.link
    def connecting(path,*a,**k):
        if str(path)==str(dest):swap()                                # reserved and closed, about to be reopened by name
        return connect(path,*a,**k)
    def linking(src,dst,*a,**k):
        if str(dst)==str(dest):swap()                                 # staged, about to be published
        return link(src,dst,*a,**k)
    monkeypatch.setattr(mod.sqlite3,'connect',connecting);monkeypatch.setattr(mod.os,'link',linking)
    with pytest.raises(FileExistsError):mod.backup(source,dest)       # was: no error, the other database overwritten
    monkeypatch.undo()
    with sqlite3.connect(victim) as c:assert c.execute('SELECT v FROM kept').fetchone()[0]=='untouched'
    assert not [p for p in tmp_path.iterdir() if p.name.startswith('.cortex-backup-')]    # the staging directory is gone

def test_a_planted_staging_entry_is_never_written_through(tmp_path,monkeypatch):
    # The staged copy is created exclusively and without following a link: an entry already in the staging directory (a
    # symlink to another database, planted by whoever controls it) refuses the backup instead of being written through.
    import os,tempfile
    mod=load_script('cortex_backup');source,dest,victim=tmp_path/'source.db',tmp_path/'backup.db',tmp_path/'victim.db'
    with sqlite3.connect(source) as c:c.execute('CREATE TABLE notes(n TEXT)')
    with sqlite3.connect(victim) as c:c.execute('CREATE TABLE kept(v TEXT)');c.execute("INSERT INTO kept VALUES('untouched')")
    stage=tmp_path/'planted';stage.mkdir()
    try:os.symlink(victim,stage/'backup.db')
    except OSError:pytest.skip('this user cannot create symlinks here')
    monkeypatch.setattr(tempfile,'mkdtemp',lambda **k:str(stage))
    with pytest.raises(FileExistsError):mod.backup(source,dest)
    monkeypatch.undo()
    with sqlite3.connect(victim) as c:assert c.execute('SELECT v FROM kept').fetchone()[0]=='untouched'
    assert not dest.exists()
