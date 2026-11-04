//! Tiny all-or-nothing staging helper used before a suspended worker resumes.
pub fn stage_handles<H: Copy, E>(
    sources: &[H],
    mut duplicate: impl FnMut(H) -> Result<H, E>,
    mut stage: impl FnMut(&[H]) -> Result<(), E>,
    mut close_remote: impl FnMut(H),
) -> Result<(), E> {
    let mut remote = Vec::with_capacity(sources.len());
    let result = (|| {
        for &source in sources {
            remote.push(duplicate(source)?);
        }
        stage(&remote)
    })();
    if result.is_err() {
        for handle in remote.into_iter().rev() {
            close_remote(handle);
        }
    }
    result
}
/// Quote a single Windows command-line argument for the standard CRT parser.
#[cfg(any(windows, test))]
pub fn quote_windows_argument(value: &[u16]) -> Vec<u16> {
    let mut result = vec![b'"' as u16];
    let mut slashes = 0;
    for &c in value {
        if c == b'\\' as u16 {
            slashes += 1;
            continue;
        }
        if c == b'"' as u16 {
            result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2 + 1));
            result.push(c);
        } else {
            result.extend(std::iter::repeat_n(b'\\' as u16, slashes));
            result.push(c);
        }
        slashes = 0;
    }
    result.extend(std::iter::repeat_n(b'\\' as u16, slashes * 2));
    result.push(b'"' as u16);
    result
}
#[cfg(test)]
mod tests {
    use super::{quote_windows_argument, stage_handles};
    use std::cell::RefCell;
    #[test]
    fn windows_argument_quoting_preserves_spaces_quotes_and_trailing_slashes() {
        let wide = |s: &str| s.encode_utf16().collect::<Vec<_>>();
        let text = |v: Vec<u16>| String::from_utf16(&v).unwrap();
        assert_eq!(text(quote_windows_argument(&wide("simple"))), "\"simple\"");
        assert_eq!(text(quote_windows_argument(&wide("a b\\"))), "\"a b\\\\\"");
        assert_eq!(text(quote_windows_argument(&wide("a\"b"))), "\"a\\\"b\"");
    }

    #[test]
    fn partial_duplication_and_staging_failure_close_every_remote_handle() {
        let closed = RefCell::new(Vec::new());
        let result = stage_handles(
            &[1, 2, 3, 4],
            |v| if v == 3 { Err(()) } else { Ok(v + 10) },
            |_| panic!("descriptor must not be staged"),
            |v| closed.borrow_mut().push(v),
        );
        assert!(result.is_err());
        assert_eq!(*closed.borrow(), vec![12, 11]);
        closed.borrow_mut().clear();
        let result = stage_handles(
            &[1, 2, 3, 4],
            |v| Ok(v + 10),
            |_| Err(()),
            |v| closed.borrow_mut().push(v),
        );
        assert!(result.is_err());
        assert_eq!(*closed.borrow(), vec![14, 13, 12, 11]);
    }
    #[test]
    fn success_stages_all_handles_and_transfers_ownership_without_closing() {
        let closed = RefCell::new(Vec::new());
        let staged = RefCell::new(Vec::new());
        stage_handles(
            &[1, 2, 3, 4],
            |v| Ok::<_, ()>(v + 10),
            |v| {
                staged.borrow_mut().extend_from_slice(v);
                Ok(())
            },
            |v| closed.borrow_mut().push(v),
        )
        .unwrap();
        assert_eq!(*staged.borrow(), vec![11, 12, 13, 14]);
        assert!(closed.borrow().is_empty());
    }
}
