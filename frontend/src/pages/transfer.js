import {store,load} from '../stores/research.js';
import {runs,transfers} from '../api/research.js';
import {table} from '../components/common/table.js';
import {esc} from '../utils/format.js';
import {download} from '../utils/download.js';
import {bindForm} from '../hooks/forms.js';
const STATE={pending:'待完成','incomplete':'留档未完成','ready':'可用'};
const OUTCOME={imported:'新导入',recovered:'恢复完成',duplicate:'重复包（复用既有研究）',detail:'',verify:'复核',list:''};
export async function render(id){
 const [list,transferId]=[await transfers.list(),Number(id)||0];
 const detail=transferId?await transfers.get(transferId):null;
 const writable=localStorage.getItem('research-role')!=='viewer';
 const completed=store.runs.filter(r=>r.status==='completed');
 return `<div class="eyebrow">06 / MIGRATION DESK</div><h1>研究迁移包</h1><p>把冻结观测、参数、结果与复核依据打进可校验的研究包；接收端逐项核对内容与关联，缺件或篡改整包拒收。导入后先落库与留档，全部完成才可见。</p>
 <section class="strip"><span>${list.length} 个接收记录</span><span>${list.filter(x=>x.status==='ready').length} 个可用</span><span>${list.filter(x=>x.status!=='ready').length} 个待恢复</span></section>
 <h2>打包发送 <small>只打包已完成研究；包内含冻结观测、参数、结果、留档附件与复核依据</small></h2>
 ${writable?`<div class="worksheet"><label>选择研究<select id="package-select">${completed.map(r=>`<option value="${r.id}">#${r.id} ${esc(r.name)} · ${r.status==='completed'?'已完成':'进行中'}${r.review?' · 已复核':' · 未复核'}</option>`).join('')||'<option value="">尚无可打包研究</option>'}</select></label><button id="package-button" ${completed.length?'':'disabled'}>下载研究包 (.zip) →</button><p>打包不改变本地记录；来源端复核在接收端仅作为依据，不冒充本地复核。</p></div>`:'<p class="method-note">观察员可查看接收记录，分析师与负责人可以打包与接收。</p>'}
 <h2>接收研究包 <small>上传后先校验整包，再进入待核验；数据库与留档全部落妥才可见</small></h2>
 ${writable?`<form id="receive-form" class="inline-form"><label>研究包文件 (.zip)<input name="package" type="file" accept=".zip,application/zip" required></label><button type="submit">上传并校验 →</button></form>`:''}
 <div id="receive-result"></div>
 <h2>接收记录</h2>
 ${table(['传输','来源','状态','本地研究','核验','操作'],list.map(x=>[{html:`<a href="#transfer/${x.id}">#${x.id}</a><br><small>${esc(x.package_id.slice(0,12))}…</small>`},{html:`${esc(x.source.study_name)}<br><small>${esc(x.source.instance_id)} / 批次 ${esc(x.source.study_id)} / 运行 ${esc(x.source.run_id)}</small>`},{html:`<span class="status ${x.status==='ready'?'completed':x.status==='pending'?'queued':'failed'}">${STATE[x.status]||x.status}</span><br><small>${esc(OUTCOME[x.outcome]||x.outcome)}</small>`},x.status==='ready'?{html:`批次 #${x.local.study_id} · 运行 #${x.local.run_id}`}:{html:'<em>暂不可见</em>'},x.verification.ok?'通过':esc(x.verification.summary||'—'),{html:`<a href="#transfer/${x.id}">详情 →</a>`}]))}
 ${detail?`<h2>接收详情 #${detail.id}</h2>
 <div class="checksum">PACKAGE ${detail.package_id}<br>SHA256 ${detail.package_sha256}</div>
 <p>来源 ${esc(detail.source.instance_id)} · 批次 ${esc(detail.source.study_id)} ${esc(detail.source.study_name)} · 运行 ${esc(detail.source.run_id)} ${esc(detail.source.run_name)}</p>
 <p>状态 ${STATE[detail.status]||detail.status} · 接收 ${esc(detail.received_at)} · 核验 ${esc(detail.verified_at||'—')}</p>
 ${detail.error?`<div class="error">${esc(detail.error)}</div>`:''}
 ${detail.source_review?`<div class="method-note"><strong>来源端复核依据（非本地复核）</strong><p>${esc(detail.source_review.note)}</p><small>${esc(detail.source_review.author)} · ${esc(detail.source_review.created_at)}</small></div>`:'<p class="method-note">来源包不含复核依据。</p>'}
 <p>留档${detail.archive_verified===true?'已核对一致':detail.archive_verified===false?'校验不一致':'待核对'}。</p>
 ${detail.status!=='ready'&&writable?`<button id="resume">重试恢复 →</button> `:''}${detail.status==='ready'?`<button id="verify" class="secondary">重新核验本地留档</button>`:''}
 <h2>逐项校验 <small>${detail.validation?.checks?.length||0} 项</small></h2>
 <p>${esc(detail.validation?.summary||'')}</p>
 ${table(['环节','对象','检查','结果','说明'],(detail.validation?.checks||[]).map(c=>[c.section,c.item,c.check,c.ok?'通过':'不通过',c.detail||'']))}
 <details><summary>清单与关联</summary><pre>${esc(JSON.stringify({source:detail.manifest?.source,relationships:detail.manifest?.relationships,items:detail.manifest?.items},null,2))}</pre></details>`:''}`;
}
export function bind(refresh){
 const button=document.getElementById('package-button');
 if(button)button.onclick=async()=>{const id=Number(document.getElementById('package-select')?.value);if(!id)return;button.disabled=true;try{const {blob,headers}=await runs.package(id);download(blob,`research-package-${id}.zip`);document.getElementById('receive-result').textContent='已下载研究包：'+(headers.get('X-Package-Id')||'');}catch(e){document.getElementById('notice').textContent=e.message;}finally{button.disabled=false;}};
 bindForm('receive-form',async(data,form)=>{
  const file=form.elements.package.files[0];
  if(!file){document.getElementById('notice').textContent='请选择研究包文件';return;}
  try{
   const result=await transfers.receive(file);
   const el=document.getElementById('receive-result');
   el.innerHTML=`<div class="method-note"><strong>${esc(OUTCOME[result.outcome]||'已接收')}</strong><p>包 ${esc(result.package_id.slice(0,16))}… · ${esc(result.verification.summary)}</p><p>传输 #${result.id} · 本地批次 #${result.local.study_id} · 本地运行 #${result.local.run_id}</p></div>`;
   await load();refresh();
  }catch(e){
   const el=document.getElementById('receive-result');
   const failures=e.detail?.report?.failures||[];
   el.innerHTML=`<div class="error"><strong>整包拒收：</strong>${esc(e.message)}</div>${failures.length?table(['对象','检查','说明'],failures.map(f=>[f.item,f.check,f.detail||''])):''}`;
  }
 });
 const resume=document.getElementById('resume');
 if(resume)resume.onclick=async()=>{resume.disabled=true;try{const x=await transfers.resume(Number(location.hash.split('/')[1]));document.getElementById('notice').textContent=`恢复${x.status==='ready'?'完成':'未完成'}：${x.error||'留档与数据库均已落妥'}`;await load();refresh();}catch(e){document.getElementById('notice').textContent=e.message;}finally{resume.disabled=false;}};
 const verify=document.getElementById('verify');
 if(verify)verify.onclick=async()=>{verify.disabled=true;try{const x=await transfers.verify(Number(location.hash.split('/')[1]));document.getElementById('notice').textContent=x.verification.summary;await load();refresh();}catch(e){document.getElementById('notice').textContent=e.message;}finally{verify.disabled=false;}};
}
