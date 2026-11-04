import {LIMIT, EXPIRES, validate, actualCost} from './policy.mjs';
type Env = {DB:D1Database; META_API_KEY:string; TRIAL_TOKEN_SHA256:string};
const json = (message:string,status=400) => Response.json({error:{message,type:'trial_error'}},{status,headers:{'Cache-Control':'no-store'}});
async function authorized(request:Request,env:Env) {
  const token = request.headers.get('Authorization')?.replace(/^Bearer /,'') || '';
  if (!env.TRIAL_TOKEN_SHA256 || token.length > 256) return false;
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token)))).map(x=>x.toString(16).padStart(2,'0')).join('');
  return hash === env.TRIAL_TOKEN_SHA256;
}
async function reserve(env:Env,amount:number) {
  const id=crypto.randomUUID(),now=Date.now();
  await env.DB.prepare('INSERT OR IGNORE INTO budget (id,used) VALUES (1,0)').run();
  const results=await env.DB.batch([
    env.DB.prepare(`INSERT INTO reservations (id,amount,created,state) SELECT ?,?,?,'reserved' FROM budget WHERE id=1 AND used+?<=? AND (SELECT count(*) FROM reservations WHERE state='reserved' AND created>?)<4 AND (SELECT count(*) FROM reservations WHERE created>?)<60`).bind(id,amount,now,amount,LIMIT,now-180000,now-60000),
    env.DB.prepare('UPDATE budget SET used=used+? WHERE id=1 AND EXISTS (SELECT 1 FROM reservations WHERE id=?)').bind(amount,id)
  ]);
  return results[0].meta.changes ? id : null;
}
async function settle(env:Env,id:string,refund:number) {
  await env.DB.batch([
    env.DB.prepare("UPDATE budget SET used=used-? WHERE id=1 AND EXISTS (SELECT 1 FROM reservations WHERE id=? AND state='reserved')").bind(refund,id),
    env.DB.prepare("UPDATE reservations SET state='done' WHERE id=? AND state='reserved'").bind(id)
  ]);
}
async function boundedBody(request:Request) {
  const reader=request.body?.getReader(); if(!reader) throw Error('Missing body');
  const chunks:Uint8Array[]=[]; let size=0;
  while(true){const {done,value}=await reader.read();if(done)break;size+=value.length;if(size>6*1024*1024){await reader.cancel();throw Error('Request too large');}chunks.push(value);}
  const bytes=new Uint8Array(size);let at=0;for(const c of chunks){bytes.set(c,at);at+=c.length;}return JSON.parse(new TextDecoder().decode(bytes));
}
export async function gateway(request:Request,env:Env,ctx:ExecutionContext):Promise<Response|null> {
  const path=new URL(request.url).pathname;
  if(!path.startsWith('/v1/') && path!='/health')return null;
  if(path==='/health')return Response.json({service:'avo-reviewer',ready:!!env.DB && !!env.META_API_KEY && !!env.TRIAL_TOKEN_SHA256,expires:'2026-11-19T00:00:00Z'});
  if(Date.now()>=EXPIRES)return json('Reviewer trial has expired. You can still use your own Meta API key.',403);
  if(!env.META_API_KEY || !env.DB)return json('Reviewer trial is not configured.',503);
  if(!await authorized(request,env))return json('Invalid reviewer invitation.',401);
  if(path==='/v1/asr/realtime' && request.headers.get('Upgrade')?.toLowerCase()==='websocket')return speech(request,env,ctx);
  if(path!='/v1/responses' || request.method!=='POST')return json('Unsupported endpoint.',404);
  let checked;try{checked=validate(await boundedBody(request));}catch(e){return json((e as Error).message);}
  const id=await reserve(env,checked.reserve);if(!id)return json('Trial quota or concurrent request limit reached. Try later, or use your own key.',429);
  try {
    const upstream=await fetch('https://api.meta.ai/v1/responses',{method:'POST',headers:{Authorization:`Bearer ${env.META_API_KEY}`,'Content-Type':'application/json'},body:JSON.stringify(checked.body),signal:AbortSignal.timeout(120000)});
    if(!upstream.ok){await settle(env,id,0);return json(`Meta request failed (${upstream.status}).`,upstream.status>=500?502:400);}
    const result=await upstream.json() as any;
    await settle(env,id,checked.reserve-actualCost(checked.body.model,result.usage,checked.reserve));
    return Response.json(result,{headers:{'Cache-Control':'no-store'}});
  }catch{await settle(env,id,0);return json('Meta request did not complete. Reserved quota was retained.',502);}
}
async function speech(request:Request,env:Env,ctx:ExecutionContext) {
  // 60 seconds of 24kHz mono PCM = $0.003. Retain this reservation even on failure.
  const id=await reserve(env,3000);if(!id)return json('Trial quota or concurrency limit reached.',429);
  let response;try{response=await fetch('https://api.meta.ai/v1/asr/realtime',{headers:{Upgrade:'websocket'},signal:AbortSignal.timeout(10000)});}catch{await settle(env,id,0);return json('Speech service unavailable.',502);}
  const upstream=response.webSocket;if(!upstream){await settle(env,id,0);return json('Speech service unavailable.',502);}
  upstream.binaryType='arraybuffer'; upstream.accept();const pair=new WebSocketPair(),client=pair[0],server=pair[1];server.binaryType='arraybuffer';server.accept();
  let configured=false,closed=false,bytes=0;
  const finish=()=>{if(closed)return;closed=true;clearTimeout(timer);try{upstream.close(1000,'Session ended');}catch{}try{server.close(1000,'Session ended');}catch{}ctx.waitUntil(settle(env,id,0));};
  const timer=setTimeout(finish,65000);
  server.addEventListener('message',(event)=>{
    try {
      if(closed)return;
      if(!configured){
        if(typeof event.data!=='string' || event.data.length>12000)return finish();
        const config=JSON.parse(event.data);
        if(config.audioEncoding!=='PCM_24KHZ')return finish();
        upstream.send(JSON.stringify({authorization:{accessToken:`Bearer ${env.META_API_KEY}`},model:'muse-voice-transcribe-1.0',audioEncoding:'PCM_24KHZ',mode:'ENDPOINTING',emitAudioProgress:false,languageBias:['English'],keywords:Array.isArray(config.keywords)?config.keywords.filter((x:any)=>typeof x==='string'&&x.length<=30).slice(0,120):[]}));
        configured=true;return;
      }
      if(typeof event.data==='string') {if(event.data==='{"type":"endStream"}')upstream.send(event.data);else finish();}
      else {if(!(event.data instanceof ArrayBuffer))return finish();bytes+=event.data.byteLength;if(bytes>60*24000*2)return finish();upstream.send(event.data);}
    }catch{finish();}
  });
  upstream.addEventListener('message',(event)=>{if(!closed){try{server.send(event.data);}catch{finish();}}});
  for(const ws of [server,upstream]){ws.addEventListener('close',finish);ws.addEventListener('error',finish);}
  return new Response(null,{status:101,webSocket:client});
}
