export function LoadState({loading,error,hasValue,retry}: {loading:boolean; error:string; hasValue:boolean; retry:()=>void}) {
  return <>{loading && <p role="status">{hasValue ? 'Updating… Last known data shown.' : 'Loading…'}</p>}{error && <div className="notice load-error" role="alert"><p>{error}{hasValue && ' Last known data shown; current authority is unconfirmed.'}</p><button onClick={retry}>Retry</button></div>}</>;
}
