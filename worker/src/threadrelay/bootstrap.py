"""Private host-to-child bootstrap. Never accepted by the command dispatcher."""
from pathlib import Path
from typing import BinaryIO

from .main import read_frame


def read_startup(channel: BinaryIO) -> dict:
    value=read_frame(channel)
    if not isinstance(value,dict): raise ValueError('Invalid private bootstrap')
    common={'version','type','data_root','generation'}
    expected=common if value.get('type')=='LocalWorkerBootstrap' else common|{'dataset','output','quantization'}
    if (set(value)!=expected or value.get('type') not in {'LocalWorkerBootstrap','LocalEvaluationBootstrap'}
            or type(value.get('version')) is not int or value['version']!=1
            or type(value.get('generation')) is not int or not 1<=value['generation']<=2**53-1):
        raise ValueError('Invalid private bootstrap')
    paths=['data_root'] if value['type']=='LocalWorkerBootstrap' else ['data_root','dataset','output']
    for field in paths:
        path=value[field]
        if not isinstance(path,str) or not 1<=len(path)<=4096 or '\0' in path or not Path(path).is_absolute():
            raise ValueError('Invalid private bootstrap')
    if value['type']=='LocalEvaluationBootstrap' and (value['quantization'] not in {'Q4_K_M','Q8_0'} or value['dataset']==value['output']):
        raise ValueError('Invalid private bootstrap')
    return value


def read_bootstrap(channel: BinaryIO) -> tuple[Path, int]:
    value=read_startup(channel)
    if value['type']!='LocalWorkerBootstrap': raise ValueError('Invalid private bootstrap')
    return Path(value['data_root']),value['generation']


def read_windows_descriptor(source):
    """Consume only fixed handle metadata; no secret or ordinary command data."""
    import struct
    from .main import _read_exact
    try:
        marker, version, count, reserved, generation, *handles = struct.unpack(
            '<4sBBHQQQQQQQ', _read_exact(source, 64))
        if (marker != b'TRHP' or version != 1 or count != 6 or reserved != 0
                or not 1 <= generation <= 2**53 - 1 or len(set(handles)) != 6
                or any(not 1 <= value < 2**64 - 1 for value in handles)):
            raise ValueError
        return generation, tuple(handles)
    except Exception:
        raise ValueError('Invalid private handle descriptor') from None
