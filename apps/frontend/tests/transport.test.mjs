import test from "node:test";
import assert from "node:assert/strict";
import {connectDashboard} from "../transport.js";

const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture() {
  let counter=0;
  const intervals=new Map(),timeouts=new Map(),sources=[];
  const env={
    fetch:async()=>({ok:true,json:async()=>({schemaVersion:2,sequence:1})}),
    setInterval:(fn,ms)=>{const id=++counter;intervals.set(id,{fn,ms});return id;},
    clearInterval:id=>intervals.delete(id),
    setTimeout:(fn,ms)=>{const id=++counter;timeouts.set(id,{fn,ms});return id;},
    clearTimeout:id=>timeouts.delete(id),
    EventSource:class {
      constructor(){this.listeners={};this.closed=false;sources.push(this);}
      addEventListener(name,callback){this.listeners[name]=callback;}
      close(){this.closed=true;}
      send(value){this.listeners.snapshot({data:value});}
    }
  };
  return {env,intervals,timeouts,sources};
}

test("healthy SSE has no background polling and failure uses one fallback",async()=>{
  const f=fixture(), snapshots=[],states=[];
  const stop=connectDashboard(s=>snapshots.push(s),s=>states.push(s),f.env);
  await flush();
  f.sources[0].send(JSON.stringify({schemaVersion:2,sequence:2}));
  assert.equal(f.intervals.size,1); // watchdog only
  assert.equal(states.at(-1),"sse");
  f.sources[0].onerror();f.sources[0].onerror();
  assert.equal(f.intervals.size,2); // watchdog plus one fallback
  assert.equal(f.sources.length,1); // native EventSource owns reconnect
  await flush();
  f.sources[0].send(JSON.stringify({schemaVersion:2,sequence:3}));
  assert.equal(f.intervals.size,1);
  stop();await flush();
  assert.equal(f.intervals.size,0);
  assert.equal(f.timeouts.size,0);
  assert.equal(f.sources[0].closed,true);
});

test("malformed SSE recovers through fallback instead of throwing",async()=>{
  const f=fixture(),states=[];
  const stop=connectDashboard(()=>{},s=>states.push(s),f.env);
  await flush();
  assert.doesNotThrow(()=>f.sources[0].send("not JSON"));
  assert.equal(states.at(-1),"offline");
  assert.equal(f.intervals.size,2);
  stop();await flush();
});

test("wrong API schema is not rendered",async()=>{
  const f=fixture(),snapshots=[],states=[];
  f.env.fetch=async()=>({ok:true,json:async()=>({schemaVersion:1})});
  const stop=connectDashboard(s=>snapshots.push(s),s=>states.push(s),f.env);
  await flush();
  assert.equal(snapshots.length,0);
  assert.equal(states.at(-1),"offline");
  stop();
});

test("stopping aborts a pending HTTP fallback",async()=>{
  const f=fixture();let signal;
  f.env.fetch=(url,options)=>{signal=options.signal;return new Promise((resolve,reject)=>signal.addEventListener("abort",()=>reject(new Error("aborted"))));};
  const stop=connectDashboard(()=>{},()=>{},f.env);
  stop();await flush();
  assert.equal(signal.aborted,true);
  assert.equal(f.timeouts.size,0);
});
