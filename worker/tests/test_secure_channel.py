"""Synthetic one-shot private handoff; no real credentials or native proof."""
import pytest
from test_telegram_auth import build, state
from threadrelay.features import AccountFeatures


def test_private_handoff_binds_generation_challenge_epoch_and_disposes(tmp_path):
    from threadrelay.secure_channel import SecureChannel
    from datetime import datetime, timezone
    db, binding, client, _, auth = build(tmp_path)
    try:
        channel = SecureChannel(generation=7)
        auth._handoff = channel
        feature = AccountFeatures(auth, vault_reference=lambda: 'vault:fixture', native_gate=lambda: True,
            discard_secret_ref=channel.discard, wall_clock=lambda: datetime.now(timezone.utc))
        feature.begin(); binding.events.append(state('WaitCode')); auth.poll()
        challenge = auth.challenge(); handle = 'a' * 64
        msg = {'type':'SubmitSecret','generation':7,'epoch':auth.snapshot().epoch,
               'challenge_id':challenge.challenge_id,'kind':'code','handle':handle,'value':'synthetic-value'}
        with pytest.raises(ValueError): channel.submit(feature, {**msg, 'generation':8})
        assert channel.pending_count == 0
        msg['handle'] = 'b' * 64
        channel.submit(feature, msg)
        assert channel.pending_count == 0
        with pytest.raises(ValueError): channel.submit(feature, msg)
        assert channel.pending_count == 0
    finally:
        db.close()


def test_qr_is_generation_epoch_challenge_bound_and_one_shot(tmp_path):
    from threadrelay.secure_channel import SecureChannel
    from datetime import datetime, timezone
    db, binding, client, _, auth = build(tmp_path)
    try:
        channel = SecureChannel(generation=3)
        feature = AccountFeatures(auth, vault_reference=lambda: 'vault:fixture', native_gate=lambda: True,
            discard_secret_ref=channel.discard, wall_clock=lambda: datetime.now(timezone.utc))
        feature.begin(); binding.events.append(state('WaitPhoneNumber')); auth.poll()
        auth.request_qr(auth.challenge().challenge_id)
        binding.events.append(state('WaitOtherDeviceConfirmation', link='synthetic-private-link')); auth.poll()
        challenge = auth.challenge()
        request = {'type':'TakeQr','generation':3,'epoch':auth.snapshot().epoch,'challenge_id':challenge.challenge_id}
        with pytest.raises(ValueError): channel.take_qr(feature, {**request, 'epoch':request['epoch']+1})
        assert channel.take_qr(feature, request) == 'synthetic-private-link'
        with pytest.raises(ValueError): channel.take_qr(feature, request)
    finally:
        db.close()


def test_private_pipe_round_trip_never_puts_input_in_response(tmp_path):
    import socket
    from threading import Thread
    from datetime import datetime, timezone
    from threadrelay.main import encode_frame, read_frame
    from threadrelay.secure_channel import SecureChannel, serve_private
    db, binding, client, _, auth = build(tmp_path)
    host, worker = socket.socketpair()
    try:
        channel = SecureChannel(generation=5); auth._handoff = channel
        feature = AccountFeatures(auth, vault_reference=lambda:'vault:fixture', native_gate=lambda:True,
            discard_secret_ref=channel.discard, wall_clock=lambda:datetime.now(timezone.utc))
        feature.begin(); binding.events.append(state('WaitCode')); auth.poll()
        stream = worker.makefile('rwb', buffering=0)
        thread = Thread(target=serve_private, args=(stream, channel, feature), daemon=True); thread.start()
        message = {'type':'SubmitSecret','generation':5,'epoch':auth.snapshot().epoch,
            'challenge_id':auth.challenge().challenge_id,'kind':'code','handle':'c'*64,'value':'synthetic-value'}
        host.sendall(encode_frame(message))
        response = read_frame(host.makefile('rb', buffering=0))
        assert response == {'type':'PrivateResult','ok':True}
        assert 'synthetic-value' not in str(response)
        host.shutdown(socket.SHUT_RDWR); host.close(); thread.join(2)
        assert not thread.is_alive()
    finally:
        worker.close(); db.close()


def test_private_pipe_rejects_oversized_frame_before_dispatch():
    from io import BytesIO
    import struct
    from threadrelay.secure_channel import SecureChannel, serve_private
    channel = SecureChannel(generation=1)
    class Duplex(BytesIO):
        pass
    stream = Duplex(struct.pack('>I', 8193))
    serve_private(stream, channel, None)
    assert stream.getvalue() == struct.pack('>I', 8193)
    assert channel.pending_count == 0
    with pytest.raises(ValueError): channel.take('anything')
