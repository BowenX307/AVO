const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const voice = require('./voice');
const root = fs.mkdtempSync(path.join(os.tmpdir(),'avo-trial-'));
const saved = {MODEL_API_KEY:process.env.MODEL_API_KEY,AVO_TRIAL_TOKEN:process.env.AVO_TRIAL_TOKEN};
try {
  delete process.env.MODEL_API_KEY; delete process.env.AVO_TRIAL_TOKEN;
  fs.writeFileSync(path.join(root,'.env'),'MODEL_API_KEY="own-key"\n');
  assert.equal(voice.apiKey(root),'own-key');
  assert.equal(voice.apiURL(root),'wss://api.meta.ai/v1/asr/realtime');
  fs.appendFileSync(path.join(root,'.env'),'AVO_TRIAL_TOKEN="invitation"\n');
  assert.equal(voice.apiKey(root),'invitation');
  assert.match(voice.apiURL(root),/^wss:\/\/avo-reviewer-gateway\./);
  console.log('Voice credential routing passed');
} finally {
  for(const [key,value] of Object.entries(saved)) { if(value===undefined)delete process.env[key];else process.env[key]=value; }
  fs.rmSync(root,{recursive:true,force:true});
}
