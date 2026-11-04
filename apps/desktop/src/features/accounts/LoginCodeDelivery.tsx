import { useEffect, useState } from 'react';
import type { AuthCodeDelivery } from '../../lib/contracts';

const names={telegram:'your Telegram app',sms:'SMS',call:'a phone call',fragment:'Fragment',unknown:'another method'};
export function LoginCodeDelivery({delivery,pending,resend}:{delivery?:AuthCodeDelivery|null;pending:boolean;resend?:()=>void}) {
 const [remaining,setRemaining]=useState(delivery?.resend_after_seconds??0);
 useEffect(()=>{
  const seconds=delivery?.resend_after_seconds??0;setRemaining(seconds);
  if(!seconds)return;
  const deadline=performance.now()+seconds*1000;
  const timer=setInterval(()=>setRemaining(Math.max(0,Math.ceil((deadline-performance.now())/1000))),1000);
  return()=>clearInterval(timer);
 },[delivery?.resend_after_seconds]);
 return <div className="login-code-help">
  <p>{delivery?.method==='telegram'?'Check the verified Telegram service chat in your Telegram app on a signed-in device.':delivery?.method==='sms'?'Telegram sent this code by SMS. Check the number you entered and your phone’s messages.':delivery?.method==='call'?'Telegram is using a phone call for this code.':delivery?.method==='fragment'?'Telegram is using Fragment for this code.':"Telegram has not provided a recognized code-delivery method. You can use QR login instead."}</p>
  {delivery?.next_method&&resend?<div className="actions"><button disabled={pending||remaining>0} onClick={resend}>{remaining>0?`Resend available in ${remaining}s`:`Resend code via ${names[delivery.next_method]}`}</button></div>:<p className="muted">Telegram has not offered another code request for this step. QR login is available below.</p>}
 </div>;
}
