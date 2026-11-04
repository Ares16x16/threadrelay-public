import { invoke as tauriInvoke } from '@tauri-apps/api/core';

export type EmbeddedBounds = {x:number;y:number;width:number;height:number};
export type EmbeddedStatus = {open:boolean;generation:number};
type Invoke = (name:string,args:Record<string,unknown>)=>Promise<unknown>;
const unavailable=()=>new Error('Embedded Threads is unavailable.');
function generation(value:number) {if(!Number.isSafeInteger(value)||value<1)throw unavailable();}
function bounds(value:EmbeddedBounds) {
  if(Object.keys(value).length!==4||!Object.values(value).every(Number.isFinite)||value.x<0||value.y<0||value.width<=0||value.height<=0||Object.values(value).some(n=>n>32768))throw unavailable();
}
function status(value:unknown):EmbeddedStatus {
  if(typeof value!=='object'||value===null||Object.keys(value).length!==2||!('open' in value)||!('generation' in value)||typeof value.open!=='boolean'||typeof value.generation!=='number'||!Number.isSafeInteger(value.generation)||value.generation<0||(value.open&&value.generation===0))throw unavailable();
  return {open:value.open,generation:value.generation};
}
/** Named local-host operations only. No URL, script, cookie or profile input. */
export class EmbeddedThreadsClient {
  private revisions=new Map<number,number>();
  private next(current:number){generation(current);const revision=(this.revisions.get(current)??0)+1;generation(revision);this.revisions.set(current,revision);return revision;}
  private async visibility(current:number,rect?:EmbeddedBounds,background=false):Promise<void>{
    if(rect)bounds(rect);const revision=this.next(current);
    try{await this.invoke(background?'embedded_threads_background':rect?'embedded_threads_show':'embedded_threads_hide',{generation:current,revision,...(rect?{bounds:rect}:{})});if(this.revisions.get(current)!==revision)throw unavailable();}catch{throw unavailable();}
  }
  async hide(current:number):Promise<void>{return this.visibility(current);}
  async background(current:number):Promise<void>{return this.visibility(current,undefined,true);}
  async show(current:number,rect:EmbeddedBounds):Promise<void>{return this.visibility(current,rect);}
  constructor(private readonly invoke:Invoke=(name,args)=>tauriInvoke(name,args)) {}
  async status():Promise<EmbeddedStatus> {try{return status(await this.invoke('embedded_threads_status',{}));}catch{throw unavailable();}}
  async open(rect:EmbeddedBounds):Promise<EmbeddedStatus> {
    bounds(rect);
    try{const result=status(await this.invoke('embedded_threads_open',{bounds:rect}));if(!result.open)throw unavailable();return result;}catch{throw unavailable();}
  }
  async resize(current:number,rect:EmbeddedBounds):Promise<void> {
    generation(current);bounds(rect);const revision=this.revisions.get(current)??0;
    try{await this.invoke('embedded_threads_resize',{generation:current,revision,bounds:rect});if((this.revisions.get(current)??0)!==revision)throw unavailable();}catch{throw unavailable();}
  }
  async close(current:number):Promise<void> {
    generation(current);
    this.next(current);
    try{await this.invoke('embedded_threads_close',{generation:current});}catch{throw unavailable();}
  }
}
