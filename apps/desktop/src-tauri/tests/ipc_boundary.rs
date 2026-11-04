use serde_json::{json, Value};
use std::io::{self, Cursor, Read};
use threadrelay_desktop::ipc::{
    decode_command, dispatch, encode_frame, read_frame, validate_contract,
};

struct Fragmented(Cursor<Vec<u8>>);
impl Read for Fragmented {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        let len = bytes.len().min(2);
        self.0.read(&mut bytes[..len])
    }
}

#[test]
fn unknown_commands_and_invalid_frames_never_reach_dispatch() {
    for frame in [
        vec![0, 0, 0, 0],
        vec![0, 16, 0, 1],
        vec![0, 0],
        vec![0, 0, 0, 2, b'{'],
        vec![0, 0, 0, 1, 255],
        encode_frame(&json!({"version":1,"id":"x","type":"RunShell","payload":{}})).unwrap(),
        encode_frame(&json!({"version":1,"id":"x","type":"Stop","payload":{"path":"/tmp"}}))
            .unwrap(),
    ] {
        assert!(decode_command(&mut Cursor::new(frame)).is_err());
    }
}

#[test]
fn rejects_duplicate_keys_before_dispatch() {
    let payload = br#"{"version":1,"id":"x","type":"Stop","payload":{},"type":"Start"}"#;
    let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
    frame.extend(payload);
    assert!(decode_command(&mut Cursor::new(frame)).is_err());
}

#[test]
fn reads_incrementally_preserves_adjacent_frames_and_clean_eof() {
    let command = json!({"version":1,"id":"x","type":"QueryState","payload":{}});
    let encoded = encode_frame(&command).unwrap();
    let mut stream = Fragmented(Cursor::new([encoded.clone(), encoded].concat()));
    assert_eq!(decode_command(&mut stream).unwrap(), Some(command.clone()));
    assert_eq!(decode_command(&mut stream).unwrap(), Some(command));
    assert!(decode_command(&mut stream).unwrap().is_none());
}

#[test]
fn enforces_exact_payload_byte_boundary() {
    let mut data = (1_048_576_u32).to_be_bytes().to_vec();
    data.push(b'"');
    data.extend(vec![b'a'; 1_048_574]);
    data.push(b'"');
    assert!(read_frame(&mut Cursor::new(data)).unwrap().is_some());
    assert!(encode_frame(&Value::String("a".repeat(1_048_575))).is_err());
}

#[test]
fn native_entrypoint_is_stopped_and_cannot_start_unimplemented_work() {
    let query = json!({"version":1,"id":"x","type":"QueryState","payload":{}});
    let state = dispatch(query.clone()).unwrap();
    assert_eq!(state["type"], "Error");
    assert_eq!(state["payload"]["code"], "unsupported");
    let start = json!({"version":1,"id":"x","type":"Start","payload":{"feed_url":"https://www.threads.com/"}});
    assert_eq!(dispatch(start).unwrap()["payload"]["code"], "unsupported");
    assert_eq!(dispatch(query).unwrap(), state);
}

#[test]
fn rust_uses_same_valid_and_invalid_contract_fixtures() {
    let fixtures: Value =
        serde_json::from_str(include_str!("../../../../contracts/fixtures.json")).unwrap();
    for case in fixtures["valid"].as_array().unwrap() {
        assert!(
            validate_contract(case["contract"].as_str().unwrap(), &case["value"]).is_ok(),
            "{}",
            case["name"]
        );
    }
    for case in fixtures["invalid"].as_array().unwrap() {
        assert!(
            validate_contract(case["contract"].as_str().unwrap(), &case["value"]).is_err(),
            "{}",
            case["name"]
        );
    }
}

#[test]
fn unconnected_native_stub_does_not_fabricate_worker_state() {
    let query = json!({"version":1,"id":"fixture","type":"QueryState","payload":{}});
    let event = dispatch(query).unwrap();
    assert_eq!(event["type"], "Error");
    assert_eq!(event["payload"]["code"], "unsupported");
}
