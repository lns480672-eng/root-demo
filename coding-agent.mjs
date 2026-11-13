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
const tools = [
  tool('read_file', '读取当前候选文件', schema({file}), args => files.get(args.file) ?? ''),
  tool('write_file', '替换一个候选文件', schema({file,content:string}), ({file,content}) => {
    if (!files.has(file) || Buffer.byteLength(content) > 24576 || content.includes(key)) {
      throw Error('invalid file');
    }
    files.set(file, content);
    return '已写入';
  }),
  tool('submit_candidate', '向 ROOT 提交候选检查。返回拒绝原因时继续修正。', schema({decision:string}), async ({decision}) => {
    if (decision.length > 4000 || decision.includes(key)) throw Error('invalid decision');
    send({kind:'candidate',files:Object.fromEntries(files),decision});
    const result = await receive();
    return result.reason;
  }),
];
const model = {...getModel('deepseek','deepseek-v4-flash'),id:'deepseek-flash',baseUrl:'https://api.deepseek.com'};
const agent = new Agent({
  initialState:{model,tools,thinkingLevel:'off',systemPrompt:request.system},
  toolExecution:'sequential',
  getApiKey:async () => key,
  streamFn:async (model, context, options) => {
    send({kind:'dispatch'});
    if (!(await receive()).ok) throw Error('dispatch rejected');
    const stream = streamSimple(model, context, {...options,maxTokens:5200,maxRetries:0});
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
