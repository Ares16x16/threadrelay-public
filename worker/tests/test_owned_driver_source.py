"""Synthetic source-level tests of the fixed Playwright driver entrypoint."""
from pathlib import Path
import struct
import subprocess


def test_owned_driver_rejects_oversized_mux_frame_without_protocol_logging(tmp_path):
    root=Path(__file__).parents[2]
    script=root/'apps/desktop/src-tauri/native/owned-driver.cjs'
    package=root/'worker/.venv/lib/python3.12/site-packages/playwright/driver/package'
    node=package.parent/'node'
    dest=tmp_path/'native/threadrelay';dest.mkdir(parents=True)
    playwright=tmp_path/'native/playwright';playwright.mkdir()
    (playwright/'package').symlink_to(package,target_is_directory=True)
    local=dest/'owned-driver.cjs';local.write_bytes(script.read_bytes())
    child=subprocess.Popen([str(node),str(local)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={})
    output,errors=child.communicate(b'\x01'+struct.pack('<I',4*1024*1024+1),timeout=5)
    assert child.returncode!=0 and output==b'' and errors==b''


def test_pinned_driver_exposes_prelaunched_browser_over_synthetic_cdp(tmp_path):
    import json
    import socket
    import threading
    from threadrelay.collector.owned_playwright import OwnedPlaywright
    root=Path(__file__).parents[2];package=root/'worker/.venv/lib/python3.12/site-packages/playwright/driver/package';node=package.parent/'node'
    dest=tmp_path/'native/threadrelay';dest.mkdir(parents=True);playwright=tmp_path/'native/playwright';playwright.mkdir()
    (playwright/'package').symlink_to(package,target_is_directory=True)
    entry=dest/'owned-driver.cjs';entry.write_bytes((root/'apps/desktop/src-tauri/native/owned-driver.cjs').read_bytes())
    child=subprocess.Popen([str(node),str(entry)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={})
    local,relay=socket.socketpair();local.settimeout(2);relay.settimeout(2);seen=[];write_lock=threading.Lock()
    def exact(source,n):
        data=b''
        while len(data)<n:
            part=source(n-len(data))
            if not part:raise EOFError
            data+=part
        return data
    def send_node(kind,body):
        with write_lock:
            child.stdin.write(bytes([kind])+struct.pack('<I',len(body))+body);child.stdin.flush()
    def from_worker():
        try:
            while True:
                size=struct.unpack('<I',exact(relay.recv,4))[0]
                send_node(1,exact(relay.recv,size))
        except Exception:pass
    def from_node():
        try:
            while True:
                header=exact(child.stdout.read,5);size=struct.unpack('<I',header[1:])[0];body=exact(child.stdout.read,size)
                if header[0]==1:relay.sendall(struct.pack('<I',size)+body);continue
                request=json.loads(body);seen.append(request['method']);result={}
                if request['method']=='Browser.getVersion':result={'product':'Chrome/156.0.8078.4','revision':'synthetic','userAgent':'Synthetic','protocolVersion':'1.3','jsVersion':'synthetic'}
                elif request['method']=='Target.getTargetInfo':result={'targetInfo':{'targetId':'browser','type':'browser','title':'','url':'','attached':False,'canAccessOpener':False}}
                response={'id':request['id'],'result':result}
                if 'sessionId' in request:response['sessionId']=request['sessionId']
                send_node(2,json.dumps(response).encode())
        except Exception:pass
    class Driver:
        def read(self,count):return local.recv(count)
        def write(self,data):local.sendall(data)
        def revoke(self):
            try:local.shutdown(socket.SHUT_RDWR)
            except OSError:pass
    a=threading.Thread(target=from_worker,daemon=True);b=threading.Thread(target=from_node,daemon=True);a.start();b.start()
    try:
        context=OwnedPlaywright(Driver(),timeout=2).start()
        assert context.browser is not None and 'Browser.getVersion' in seen
        context.stop()
    finally:
        local.close();relay.close();child.kill();child.wait(timeout=2)
