/* Editable functional chart. No invented tempo, beat phase, or labels for unknown cells. */
window.ScoreUI = (() => {
  const $ = id => document.getElementById(id);
  let score = null, revision = -1, session = -1, loading = false, page = 0, editedBeat = 0;
  const roles = {tonic:'主功能', predominant:'下属功能', dominant:'属功能'};
  const busy = () => ['starting','listening','demo','stopping'].includes(window.liveStatus);
  async function request(path, body) {
    const r = await fetch(path, body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
    const data = await r.json(); if (!r.ok) throw Error(data.error || '请求失败'); return data;
  }
  function name(label) {
    if (!label || label === 'N') return label === 'N' ? 'N' : '';
    const [r,q] = label.split(':');
    const suffix = {maj:'', min:'m', maj7:'maj7', min7:'m7', min6:'m6', hdim7:'ø7', dim7:'°7', dim:'°', aug:'+', 'sus4(b7)':'7sus4','maj(9)':'add9','min(9)':'madd9'};
    return r + (suffix[q] ?? q);
  }
  // Compare original beat data, including across bar and page boundaries.
  // Unknown beats break a run; slash-bass changes start a new symbol.
  function isContinuation(cells, i) {
    const c=cells[i], prev=cells[i-1];
    return Boolean(c?.chord && prev?.chord === c.chord && (prev.bass || null) === (c.bass || null));
  }
  function describe(c) {
    if (!c.numeral) return '';
    if (c.role === 'secondary_dominant') return (c.numeral.startsWith('vii') ? '副导和弦 → ' : '副属和弦 → ') + (c.target || '');
    if (c.role === 'borrowed') return '同主音调式借用';
    if (c.role === 'chromatic') return '调外和弦';
    return roles[c.function] || '';
  }
  function showError(error) { $('scoreMessage').textContent = error.message; }
  function node(tag, className, text) { const n=document.createElement(tag); n.className=className; n.textContent=text; return n; }
  function render() {
    if (!score) return;
    const perBar = score.meter ? Number(score.meter.split('/')[0]) : 4;
    const configured = score.bpm != null && score.meter != null;
    const bars = Math.max(4, Math.ceil(score.cells.length/perBar));
    const pages = Math.max(1, Math.ceil(bars/16)); page = Math.min(page,pages-1);
    $('scorePage').textContent = `${page+1} / ${pages}`;
    $('prevPage').disabled=page===0; $('nextPage').disabled=page===pages-1;
    $('scoreSummary').textContent = `${score.bpm ?? '　'} BPM · ${score.meter ?? '拍号待定'} · ${score.key || score.global_key?.label || '调性待解析'}${score.global_key?.source==='estimated'?'（估计）':''}`;
    $('analysisStatus').textContent=score.analysis_status==='complete'?'级数与功能已补全':'级数与功能待解析';
    $('analyzeScore').disabled=busy() || !configured;
    $('scoreMessage').textContent=configured?'连续相同和弦仅标起点；N 表示无可确认和弦，未解析位置留空。': '请填写速度和拍号以建立小节，可点击逐拍填入。';
    $('scoreExport').disabled=!score.cells.length;
    const fragment=document.createDocumentFragment();
    for(let bar=page*16; bar<Math.min(bars,page*16+16); bar++) {
      const box=node('div','score-bar',''); box.append(node('div','bar-number',String(bar+1).padStart(2,'0')));
      const beats=node('div','score-beats',''); beats.style.gridTemplateColumns=`repeat(${perBar},minmax(0,1fr))`;
      for(let beat=0;beat<perBar;beat++) {
        const i=bar*perBar+beat; const c=score.cells[i]||{beat:i};
        const cell=node('button','score-cell'+(c.source==='manual'?' manual':''),'');
        cell.type='button'; cell.disabled=!configured || i>=score.limit;
        cell.setAttribute('aria-label',`第 ${bar+1} 小节第 ${beat+1} 拍 ${name(c.chord)||'空白'}`);
        cell.title=c.key?`调性：${c.key}；${c.source==='manual'?'手动填写':'实时预览'}`:'点击填写或修改和弦';
        const continuation=isContinuation(score.cells,i);
        const region=score.key_regions.find(r=>r.start_beat===i&&i>0);const explanation=region?`调性区域 → ${region.label} · ${describe(c)}`:(continuation?'':describe(c));
        cell.append(node('span','functional-note',explanation),node('span','degree',continuation?'':(c.numeral||'')),node('span','score-chord',continuation?'':name(c.chord)+(c.bass?'/'+c.bass:'')),node('span','beat-number',String(beat+1)+(c.source==='manual'?' · 手动':'')));
        cell.onclick=()=>openCell(i,c,bar,beat); beats.append(cell);
      }
      box.append(beats);fragment.append(box);
    }
    $('scoreGrid').replaceChildren(fragment);
  }
  function openCell(i,c,bar,beat) {
    editedBeat=i; $('editTitle').textContent=`第 ${bar+1} 小节 · 第 ${beat+1} 拍`;
    $('editChord').value=c.chord?(c.chord+(c.bass?'/'+c.bass:'')):'';
    $('editMessage').textContent=''; $('cellEditor').showModal(); $('editChord').focus();
  }
  async function edit(reset=false) {
    try {score=await request('/api/score/edit',{beat:editedBeat,chord:$('editChord').value,reset});revision=score.revision;$('cellEditor').close();render();}
    catch(e){$('editMessage').textContent=e.message;}
  }
  async function sync(nextRevision,nextSession) {
    if(loading || (revision===nextRevision&&session===nextSession))return;
    loading=true;
    try {if(session!==nextSession){page=0;session=nextSession;}const first=score===null;score=await request('/api/score');revision=score.revision;
      if(first){$('scoreBpm').value=score.bpm??'';$('scoreMeter').value=score.meter??'';$('scoreOffset').value=score.offset;$('scoreKey').value=score.key??'';}render();}
    catch(e){showError(e);}finally{loading=false;}
  }
  async function configure() {
    const bpm=$('scoreBpm').value, meter=$('scoreMeter').value;
    try {score=await request('/api/score/config',{bpm:bpm===''?null:Number(bpm),meter:meter||null,offset:Number($('scoreOffset').value||0),key:$('scoreKey').value||null});revision=score.revision;render();}
    catch(e){showError(e);}
  }
  function init() {
    for(const root of ['C','C#','D','D#','E','F','F#','G','G#','A','A#','B'])
      for(const mode of ['maj','min'])$('scoreKey').append(new Option(root+(mode==='maj'?' 大调':' 小调'),root+':'+mode));
    $('applyScore').onclick=configure;
    $('prevPage').onclick=()=>{page--;render();};$('nextPage').onclick=()=>{page++;render();};
    $('saveCell').onclick=()=>edit(false);$('resetCell').onclick=()=>edit(true);
    $('closeCell').onclick=()=>$('cellEditor').close();
    $('editChord').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();edit(false);}};
    $('analyzeScore').onclick=async()=>{try{score=await request('/api/score/analyze',{});revision=score.revision;render();}catch(e){showError(e);}};
    $('scoreExport').onclick=()=>{const url=URL.createObjectURL(new Blob([JSON.stringify(score,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='harmonic-atlas-score-'+Date.now()+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  }
  return {init,sync,name,describe,isContinuation};
})();
