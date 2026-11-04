import { useEffect, useRef, useState } from 'react';
/** Reusable credentials live only in uncontrolled fields until the native
 * secure-vault handoff; both fields clear before any asynchronous work. */
export function SecureCredentials({onSubmit,onCancel}:{onSubmit:(apiId:string,apiHash:string)=>Promise<void>;onCancel:()=>void}) {
  const id=useRef<HTMLInputElement>(null),hash=useRef<HTMLInputElement>(null),locked=useRef(false),mounted=useRef(true),[pending,setPending]=useState(false),[error,setError]=useState('');
  const clear=()=>{if(id.current)id.current.value='';if(hash.current)hash.current.value='';};
  useEffect(()=>{mounted.current=true;const a=id.current,b=hash.current;return()=>{mounted.current=false;if(a)a.value='';if(b)b.value='';};},[]);
  return <form onSubmit={async e=>{e.preventDefault();if(locked.current||!id.current||!hash.current){clear();return;}const apiId=id.current.value,apiHash=hash.current.value;clear();if(!/^\d{1,10}$/.test(apiId)||Number(apiId)<1||Number(apiId)>2147483647||!/[\da-fA-F]{32}/.test(apiHash)||apiHash.length!==32){setError('Enter a valid API ID and a 32-character hexadecimal API hash.');return;}locked.current=true;setPending(true);setError('');try{await onSubmit(apiId,apiHash);}catch{if(mounted.current)setError('Secure setup could not be confirmed. Refresh account status before retrying.');}finally{locked.current=false;if(mounted.current)setPending(false);}}}>
    <label>Developer API ID<input ref={id} disabled={pending} inputMode="numeric" autoComplete="off" maxLength={10} /></label><label>Developer API hash<input ref={hash} disabled={pending} type="password" autoComplete="off" maxLength={32} /></label>
    {error && <p className="error" role="alert">{error}</p>}<div className="actions"><button className="primary" disabled={pending}>{pending?'Storing…':'Store securely and continue'}</button><button type="button" onClick={()=>{clear();onCancel();}}>Cancel</button></div>
  </form>;
}
