import { createInterface } from 'node:readline';
import { pathToFileURL } from 'node:url';
import { join } from 'node:path';

const input = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const receive = async () => JSON.parse((await input.next()).value);
const send = value => process.stdout.write(JSON.stringify(value) + '\n');
const request = await receive();
const modules = request.modules;
const [{ Agent }, { getModel, streamSimple }] = await Promise.all(
  ['pi-agent-core', 'pi-ai'].map(name => import(pathToFileURL(join(modules, name, 'dist/index.js'))))
);
const key = process.env.DEEPSEEK_API_KEY;
delete process.env.DEEPSEEK_API_KEY;
const files = new Map(Object.entries(request.files));
const fields = Object.keys(request.files);
const schema = properties => ({ type:'object', properties, required:Object.keys(properties), additionalProperties:false });
const file = { type:'string', enum:fields };
const string = { type:'string' };
let operations = 0;
const tool = (name, description, parameters, handler) => ({
  name, label:name, description, parameters,
  async execute(_, args) {
    if (++operations > 32) throw Error('tool budget');
    let text;
    try { text = await handler(args); } catch { text = '操作被拒绝'; }
    return { content:[{ type:'text', text }], details:{} };
  }
});
async function submit(files, decision) {
  if (JSON.stringify(decision).includes(key)) throw Error('invalid decision');
  const bytes = [...files.values()].reduce((sum, content) => sum + Buffer.byteLength(content), 0);
  if (bytes > request.maxSourceBytes && !['clarify', 'unsupported'].includes(decision.status)
      && !['backfill', 'title', 'table_filter'].includes(decision.operation?.kind)) {
    return 'HTML、JavaScript 和 CSS 合计超过源码容量。';
  }
  send({kind:'candidate',files:Object.fromEntries(files),decision});
  return (await receive()).reason;
}
const tools = [
  tool('read_file', '读取当前候选文件', schema({file}), args => files.get(args.file) ?? ''),
  tool('submit_candidate', '提交候选。markup、script、style 是完整文件内容，省略未改部分。', {
    type:'object',
    properties:{
      markup:string, script:string, ...(fields.includes('tool.css') ? {style:string} : {}),
      status:{type:'string',enum:['proposed','clarify','unsupported']}, question:string, reason:string,
      field:string, choices:{type:'array',items:string}, field_label:string,
      field_type:{type:'string',enum:['text','integer','date']}, type_evidence:string,
    },additionalProperties:false,
  }, async ({markup,script,style,field_label,field_type,type_evidence,...decision}) => {
    if (field_label && field_type) {
      decision.operation = {kind:'add_field',label:field_label,type:field_type,required:false};
      decision.type_evidence = type_evidence;
    }
    const changes = {'tool.html':markup,'tool.js':script,'tool.css':style};
    const changed = new Map(files);
    for (const [name, content] of Object.entries(changes)) {
      if (content === undefined) continue;
      if (!files.has(name) || content.includes(key)) throw Error('invalid file');
      changed.set(name,content);
    }
    return submit(changed, decision);
  }),
];
if (request.tableRevision) {
  tools.push(tool('propose_table_edit', '提交一个表格修改。title 填写新标题，column 填写筛选列的序号。二选一。', {
    type:'object', properties:{title:string, column:{type:'integer',minimum:0}}, additionalProperties:false,
  }, ({title,column}) => {
    if ((title === undefined) === (column === undefined)) throw Error('select one table operation');
    const operation = title === undefined ? {kind:'table_filter',column} : {kind:'title',label:title};
    return submit(files, {operation});
  }));
}
if (request.recordRevision) {
  tools.push(tool('propose_backfill', '为已有工具提出空值回填方案。仅生成提案，由宿主计算影响并在用户确认后执行。',
    schema({field:string,value:{anyOf:[string,{type:'integer'}]}}),
    ({field,value}) => submit(files, {operation:{kind:'backfill',field,value}})));
  tools.push(tool('propose_type_change', '提出字段类型变更。提供修改后的完整 HTML 和 JavaScript，宿主计算旧值转换方案。',
    schema({field:string,type:{type:'string',enum:['text','integer','date']},markup:string,script:string}),
    ({field,type,markup,script}) => {
      const changed = new Map(files);
      for (const [name, content] of [['tool.html',markup],['tool.js',script]]) {
        if (content.includes(key)) throw Error('invalid file');
        changed.set(name,content);
      }
      return submit(changed, {operation:{kind:'change_type',field,type}});
    }));
}
const model = {...getModel('deepseek','deepseek-v4-flash'),id:'deepseek-flash',baseUrl:'https://api.deepseek.com',
  contextWindow:1048576,maxTokens:393216};
const agent = new Agent({
  initialState:{model,tools,thinkingLevel:'off',systemPrompt:request.system},
  toolExecution:'sequential',
  getApiKey:async () => key,
  streamFn:async (model, context, options) => {
    send({kind:'dispatch'});
    if (!(await receive()).ok) throw Error('dispatch rejected');
    const stream = streamSimple(model, context, {...options,toolChoice:'required',maxTokens:request.maxOutputTokens,maxRetries:0});
    stream.result().then(message => send({
      kind:'receipt', status:['error','aborted','length'].includes(message.stopReason) ? 'provider_error' : 'ok',
      usage:{total_tokens:message.usage.totalTokens}, finish_reason:message.stopReason
    }));
    return stream;
  },
});
try { await agent.prompt(JSON.stringify(request.task)); }
catch { /* The host handles missing or rejected candidates. */ }
send({kind:'done'});
