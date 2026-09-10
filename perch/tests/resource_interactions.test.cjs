/* Exercise async UI ownership without a browser or real harness configuration. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../src/perch/static/app.js'),'utf8');
const code=source.slice(source.indexOf('const resourceKinds='),source.indexOf('async function resolveSkill('));
function fixture() {
  const requests=[],notices=[];
  const panel={innerHTML:'',hidden:true,getBoundingClientRect:()=>({bottom:20})};
  const detail={scrollBy(){},getBoundingClientRect:()=>({bottom:100})};
  const context=vm.createContext({state:{tools:{targets:[{id:'codex',name:'Codex'},{id:'omp',name:'omp'}]},resourceId:'skill-a',resourceReview:null},
    $:selector=>selector==='#resource-detail'?detail:selector==='#resource-detail-title'?null:panel,
    esc:String,matchMedia:()=>({matches:true}),toast:text=>notices.push(text),
    api:(url,body)=>new Promise((resolve,reject)=>requests.push({url,body,resolve,reject}))});
  vm.runInContext(code,context);
  let refreshes=0;context.loadResources=async()=>{refreshes++;};
  return {context,requests,notices,panel,refreshes:()=>refreshes};
}
test('a slow review cannot replace a newer destination or reopen a cancelled review',async()=>{
  const f=fixture(),c=f.context;
  const first=c.reviewConnection('skill-a','codex'),second=c.reviewConnection('skill-a','omp');
  f.requests[1].resolve({status:'available',revision:'omp-revision'});await second;
  f.requests[0].resolve({status:'available',revision:'old-codex-revision'});await first;
  assert.equal(c.state.resourceReview.target,'omp');assert.equal(c.state.resourceReview.plan.revision,'omp-revision');
  const pending=c.reviewConnection('skill-a','codex');c.state.resourceReview=null;c.paintConnectionReview();
  f.requests[2].resolve({status:'available',revision:'late'});await pending;
  assert.equal(c.state.resourceReview,null);assert.equal(f.panel.hidden,true);
});
test('repeated Apply submits once using the reviewed resource, destination, and revision',async()=>{
  const f=fixture(),c=f.context;
  c.state.resourceReview={id:'skill-a',target:'codex',plan:{status:'available',revision:'checked-revision'}};
  const first=c.applyResourceConnection(),second=c.applyResourceConnection();
  assert.equal(f.requests.length,1);
  assert.equal(JSON.stringify(f.requests[0].body),JSON.stringify({id:'skill-a',target:'codex',revision:'checked-revision',apply:true}));
  // Navigating to another resource while the request runs must preserve its review.
  const next={id:'skill-b',target:'omp',plan:{status:'available',revision:'other'}};
  c.state.resourceId='skill-b';c.state.resourceReview=next;
  f.requests[0].resolve({applied:true});await Promise.all([first,second]);
  assert.equal(c.state.resourceReview,next);assert.equal(f.refreshes(),1);assert.deepEqual(f.notices,['Connected']);
});
test('failed linking keeps the review retryable and reports the error',async()=>{
  const f=fixture(),c=f.context;
  const review={id:'skill-a',target:'codex',plan:{status:'available',revision:'checked'}};c.state.resourceReview=review;
  const pending=c.applyResourceConnection();f.requests[0].reject(new Error('Source changed; review again.'));await pending;
  assert.equal(c.state.resourceReview,review);assert.equal(review.busy,false);
  assert.equal(review.error,'Source changed; review again.');assert.match(f.panel.innerHTML,/Source changed/);
  assert.equal(f.refreshes(),0);assert.deepEqual(f.notices,[]);
});
