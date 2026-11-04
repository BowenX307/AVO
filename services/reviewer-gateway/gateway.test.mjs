import test from 'node:test';
import assert from 'node:assert/strict';
import {DatabaseSync} from 'node:sqlite';
import {readFileSync} from 'node:fs';
import {validate,actualCost,LIMIT} from './policy.mjs';
const {gateway}=await import('./gateway.ts');
function database() {
 const db=new DatabaseSync(':memory:');db.exec(readFileSync(new URL('./schema.sql',import.meta.url),'utf8'));
 const wrapper={prepare(sql){let args=[];return {bind(...a){args=a;return this;},async run(){const r=db.prepare(sql).run(...args);return {meta:{changes:Number(r.changes)}};}};},async batch(statements){db.exec('BEGIN IMMEDIATE');try{const r=[];for(const s of statements)r.push(await s.run());db.exec('COMMIT');return r;}catch(e){db.exec('ROLLBACK');throw e;}}};
 return {DB:wrapper,db};
}
const token='test-invite';const hash=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(token))).toString('hex');
function request(body,auth=token){return new Request('https://test/v1/responses',{method:'POST',headers:{Authorization:`Bearer ${auth}`},body:JSON.stringify(body)});}
const simple={model:'muse-spark-1.3',input:'hello'};
test('allowlist blocks paid tools, background execution, audio and alternate models',()=>{
 for(const b of [{...simple,model:'muse-image-1.0'},{...simple,background:true},{...simple,tools:[{type:'web_search'}]},{...simple,input:[{type:'input_video',video_url:'x'}]}])assert.throws(()=>validate(b));
 assert.equal(validate({...simple,max_output_tokens:999999}).body.max_output_tokens,8192);
 assert.equal(validate(simple).reserve,2000000);
 assert.equal(validate({...simple,model:'muse-wonder-1.0'}).reserve,5000000);
 assert.throws(()=>validate({model:'sam-3.1',input:[{role:'user',content:[{type:'input_image',image_url:'https://example.com/a.png'}]}]}));
});
test('invalid usage retains the full reserve',()=>{
 assert.equal(actualCost(simple.model,null,2000000),2000000);
 assert.equal(actualCost(simple.model,{input_tokens:-1,output_tokens:2},2000000),2000000);
 assert.equal(actualCost(simple.model,{input_tokens:100,output_tokens:10},2000000),168);
});
test('unauthorized calls never reach Meta; atomic budget denies near the limit',async()=>{
 const {DB,db}=database(),env={DB,META_API_KEY:'server-only',TRIAL_TOKEN_SHA256:hash};
 assert.equal((await gateway(request(simple,'wrong'),env,{})).status,401);
 db.exec(`INSERT INTO budget VALUES (1,${LIMIT-1000000})`);
 assert.equal((await gateway(request(simple),env,{})).status,429);
 assert.equal(db.prepare('SELECT count(*) n FROM reservations').get().n,0);
});
test('successful calls meter actual usage and forward only the server key',async()=>{
 const {DB,db}=database(),env={DB,META_API_KEY:'server-only',TRIAL_TOKEN_SHA256:hash};
 const original=globalThis.fetch;
 globalThis.fetch=async(url,init)=>{assert.equal(url,'https://api.meta.ai/v1/responses');assert.equal(init.headers.Authorization,'Bearer server-only');assert.equal(JSON.parse(init.body).max_output_tokens,8192);return Response.json({id:'response1',usage:{input_tokens:100,output_tokens:10}});};
 try {const r=await gateway(request(simple),env,{});assert.equal(r.status,200);assert.equal(db.prepare('SELECT used FROM budget').get().used,168);assert.equal(db.prepare('SELECT state FROM reservations').get().state,'done');}finally{globalThis.fetch=original;}
});
test('upstream timeout conservatively retains reserved charges',async()=>{
 const {DB,db}=database(),env={DB,META_API_KEY:'server-only',TRIAL_TOKEN_SHA256:hash},original=globalThis.fetch;
 globalThis.fetch=async()=>{throw Error('timeout');};
 try{assert.equal((await gateway(request(simple),env,{})).status,502);assert.equal(db.prepare('SELECT used FROM budget').get().used,2000000);}finally{globalThis.fetch=original;}
});
test('active reservation and per-minute limits fail closed',async()=>{
 const {DB,db}=database(),env={DB,META_API_KEY:'server-only',TRIAL_TOKEN_SHA256:hash};
 db.exec('INSERT INTO budget VALUES(1,0)');
 for(let i=0;i<4;i++)db.prepare('INSERT INTO reservations VALUES(?,?,?,?)').run(String(i),2000000,Date.now(),'reserved');
 assert.equal((await gateway(request(simple),env,{})).status,429);
});
