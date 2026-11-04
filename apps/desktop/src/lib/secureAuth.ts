import { invoke as tauriInvoke } from '@tauri-apps/api/core';
import type { AuthChallenge } from './contracts';

type Invoke = (name: string, args: Record<string, unknown>) => Promise<unknown>;
export type SecureStatus = {available:boolean;configured:boolean;reason?:'app_not_ready'|'vault_unavailable'|'telegram_runtime_unavailable'|null};
/** Dedicated native actions, never ordinary worker Command/Event or React state. */
export class SecureAuthClient {
  available = false; // Updated only by the current host proof capability, never a renderer flag.
  constructor(private readonly invoke: Invoke = (name, args) => tauriInvoke(name, args)) {}
  async status():Promise<SecureStatus> {
    try {
      const value=await this.invoke('secure_status', {});
      if(typeof value!=='object'||value===null||Object.keys(value).some(key=>!['available','configured','reason'].includes(key))||!('available' in value)||!('configured' in value)||typeof value.available!=='boolean'||typeof value.configured!=='boolean')throw new Error('Unavailable');
      const hasReason='reason' in value;
      if(hasReason && value.reason!==null && !['app_not_ready','vault_unavailable','telegram_runtime_unavailable'].includes(value.reason as string))throw new Error('Unavailable');
      if(value.available && hasReason && value.reason!==null)throw new Error('Unavailable');
      this.available=value.available;
      return {available:value.available,configured:value.configured,...(hasReason?{reason:value.reason as SecureStatus['reason']}:{})};
    } catch { this.available=false; throw new Error('Secure status unavailable.'); }
  }
  async submit(challenge: AuthChallenge, epoch: number, value: string) {
    if (challenge.kind === 'qr') throw new Error('Secure submission unavailable.');
    try { await this.invoke('secure_submit', {challengeId:challenge.challenge_id,kind:challenge.kind,epoch,value}); }
    catch { throw new Error('Secure submission unavailable.'); }
  }
  async takeQr(challengeId:string, epoch:number):Promise<string> {
    try {
      const value=await this.invoke('secure_qr', {challengeId,epoch});
      if(typeof value!=='string' || !value.length || value.length>4096) throw new Error('unavailable');
      return value;
    } catch { throw new Error('Private QR unavailable.'); }
  }
  async configure(apiId: string, apiHash: string) {
    try { await this.invoke('secure_configure', {apiId,apiHash}); }
    catch { throw new Error('Secure setup unavailable.'); }
  }
}
