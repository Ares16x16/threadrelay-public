//! Private framed protocol and closed schema boundary. No process or network APIs.
use serde::{
    de::{self, MapAccess, SeqAccess, Visitor},
    Deserialize, Deserializer,
};
use serde_json::{json, Map, Value};
use std::{collections::HashMap, fmt, io::Read, sync::OnceLock};

pub const MAX_FRAME_BYTES: usize = 1_048_576;

#[derive(Debug)]
pub struct ProtocolError(&'static str);
impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.0)
    }
}
impl std::error::Error for ProtocolError {}

// Reject duplicates recursively instead of allowing last-key-wins parsing.
struct UniqueValue(Value);
impl<'de> Deserialize<'de> for UniqueValue {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct UniqueVisitor;
        impl<'de> Visitor<'de> for UniqueVisitor {
            type Value = UniqueValue;
            fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
                f.write_str("JSON without duplicate keys")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::Number(v.into())))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::Number(v.into())))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> Result<Self::Value, E> {
                serde_json::Number::from_f64(v)
                    .map(|n| UniqueValue(Value::Number(n)))
                    .ok_or_else(|| E::custom("Invalid number"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::String(v.into())))
            }
            fn visit_string<E: de::Error>(self, v: String) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::String(v)))
            }
            fn visit_unit<E: de::Error>(self) -> Result<Self::Value, E> {
                Ok(UniqueValue(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> Result<Self::Value, E> {
                self.visit_unit()
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Self::Value, A::Error> {
                let mut items = Vec::new();
                while let Some(UniqueValue(v)) = seq.next_element()? {
                    items.push(v);
                }
                Ok(UniqueValue(Value::Array(items)))
            }
            fn visit_map<A: MapAccess<'de>>(self, mut map: A) -> Result<Self::Value, A::Error> {
                let mut items = Map::new();
                while let Some((key, UniqueValue(value))) =
                    map.next_entry::<String, UniqueValue>()?
                {
                    if items.insert(key, value).is_some() {
                        return Err(de::Error::custom("Duplicate JSON key"));
                    }
                }
                Ok(UniqueValue(Value::Object(items)))
            }
        }
        deserializer.deserialize_any(UniqueVisitor)
    }
}

fn validators() -> &'static HashMap<String, jsonschema::Validator> {
    static VALIDATORS: OnceLock<HashMap<String, jsonschema::Validator>> = OnceLock::new();
    VALIDATORS.get_or_init(|| {
        let schema: Value =
            serde_json::from_str(include_str!("../../../../contracts/protocol.schema.json"))
                .expect("Bundled protocol schema must parse");
        schema["definitions"]
            .as_object()
            .expect("Bundled definitions")
            .keys()
            .map(|name| {
                let mut selected = schema.clone();
                selected["$ref"] = Value::String(format!("#/definitions/{name}"));
                let validator = jsonschema::options()
                    .with_draft(jsonschema::Draft::Draft7)
                    .should_validate_formats(true)
                    .build(&selected)
                    .expect("Bundled schema must compile");
                (name.clone(), validator)
            })
            .collect()
    })
}

pub fn validate_contract(name: &str, value: &Value) -> Result<(), ProtocolError> {
    let validator = validators()
        .get(name)
        .ok_or(ProtocolError("Unknown contract"))?;
    if validator.is_valid(value) {
        Ok(())
    } else {
        Err(ProtocolError("Invalid protocol contract"))
    }
}

pub fn read_frame<R: Read>(reader: &mut R) -> Result<Option<Value>, ProtocolError> {
    let mut header = [0u8; 4];
    // A zero-byte read is clean EOF only before any prefix byte.
    loop {
        match reader.read(&mut header[..1]) {
            Ok(0) => return Ok(None),
            Ok(_) => break,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(_) => return Err(ProtocolError("Frame read failed")),
        }
    }
    reader
        .read_exact(&mut header[1..])
        .map_err(|_| ProtocolError("Truncated frame prefix"))?;
    let size = u32::from_be_bytes(header) as usize;
    if !(1..=MAX_FRAME_BYTES).contains(&size) {
        return Err(ProtocolError("Invalid frame size"));
    }
    let mut payload = vec![0; size];
    reader
        .read_exact(&mut payload)
        .map_err(|_| ProtocolError("Truncated frame payload"))?;
    let UniqueValue(value) =
        serde_json::from_slice(&payload).map_err(|_| ProtocolError("Invalid frame JSON"))?;
    Ok(Some(value))
}

pub fn encode_frame(value: &Value) -> Result<Vec<u8>, ProtocolError> {
    let payload = serde_json::to_vec(value).map_err(|_| ProtocolError("Invalid JSON value"))?;
    if !(1..=MAX_FRAME_BYTES).contains(&payload.len()) {
        return Err(ProtocolError("Invalid frame size"));
    }
    let mut result = (payload.len() as u32).to_be_bytes().to_vec();
    result.extend(payload);
    Ok(result)
}

pub fn decode_command<R: Read>(reader: &mut R) -> Result<Option<Value>, ProtocolError> {
    let command = read_frame(reader)?;
    if let Some(value) = &command {
        validate_contract("Command", value)?;
    }
    Ok(command)
}

pub fn dispatch(command: Value) -> Result<Value, ProtocolError> {
    validate_contract("Command", &command)?;
    // Renderer calls must also meet the byte ceiling.
    encode_frame(&command)?;
    // No owned worker is connected in this scaffold. Returning a synthetic
    // State would misrepresent disconnected authority as a real worker result.
    let event = json!({"version":1,"id":command["id"],"type":"Error","payload":{
        "code":"unsupported","message":"Worker connection is not available in this build."
    }});
    validate_contract("Event", &event)?;
    Ok(event)
}
