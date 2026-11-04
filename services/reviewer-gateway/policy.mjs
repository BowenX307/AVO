export const LIMIT = 80_000_000; // USD microdollars; $20 of the approved $100 remains unallocated.
export const EXPIRES = Date.parse('2026-11-19T00:00:00Z');
const ALLOWED = new Set(['model','input','instructions','tools','tool_choice','parallel_tool_calls','reasoning','text','temperature','top_p','max_output_tokens','previous_response_id','truncation','store','include','metadata']);
export function validate(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) throw Error('Invalid request');
  for (const key of Object.keys(body)) if (!ALLOWED.has(key)) throw Error('Unsupported request option');
  if (!['muse-spark-1.3','muse-wonder-1.0','sam-3.1'].includes(body.model)) throw Error('Unsupported model');
  if (body.tools?.some(t => !['computer','computer_use_preview','function'].includes(t.type))) throw Error('Unsupported tool');
  let images = 0;
  function visit(x) {
    if (!x || typeof x !== 'object') return;
    if (Array.isArray(x)) {x.forEach(visit); return;}
    if (['input_video','input_audio','input_file'].includes(x.type)) throw Error('Only text and screenshots are supported');
    if (x.type === 'input_image') {
      images++;
      if (typeof x.image_url !== 'string' || !/^data:image\/(png|jpeg);base64,[A-Za-z0-9+/=]+$/.test(x.image_url)) throw Error('Screenshots must be inline PNG/JPEG');
    }
    Object.values(x).forEach(visit);
  }
  visit(body.input);
  if (body.model === 'sam-3.1') {
    if (images !== 1 || body.previous_response_id || body.tools || !Array.isArray(body.input) || body.input.length !== 1) throw Error('SAM requires exactly one inline image');
    const item = body.input[0];
    if (Object.keys(item).some(k => !['role','content'].includes(k)) || item.role !== 'user' || !Array.isArray(item.content) || item.content.some(c => !['input_text','input_image'].includes(c.type))) throw Error('Invalid SAM image input');
    for (const c of item.content) if(Object.keys(c).some(k => !(c.type === 'input_image' ? ['type','image_url'] : ['type','text']).includes(k))) throw Error('Invalid SAM content');
    return {body:{model:body.model,input:body.input,metadata:{include_confidence:'true'}},reserve:2500};
  }
  if (body.max_output_tokens !== undefined && (!Number.isInteger(body.max_output_tokens) || body.max_output_tokens < 1)) throw Error('Invalid output limit');
  // Worst-case full 1,048,576-token input plus capped output at published Nov 4 rates.
  // No paid hosted tools, streaming, background tasks or arbitrary endpoints.
  const reserve = body.model === 'sam-3.1' ? 2500 : body.model === 'muse-wonder-1.0' ? 5_000_000 : 2_000_000;
  return {body:{...body,max_output_tokens:Math.min(body.max_output_tokens || 8192,8192)},reserve};
}
export function actualCost(model, usage, reserve) {
  if (model === 'sam-3.1') return reserve;
  if (!usage || !Number.isSafeInteger(usage.input_tokens) || !Number.isSafeInteger(usage.output_tokens) || usage.input_tokens < 0 || usage.output_tokens < 0) return reserve;
  const cost = Math.ceil(usage.input_tokens * (model === 'muse-wonder-1.0' ? 4 : 1.25) + usage.output_tokens * (model === 'muse-wonder-1.0' ? 20 : 4.25));
  return cost <= reserve ? cost : reserve; // missing/invalid usage never refunds.
}
