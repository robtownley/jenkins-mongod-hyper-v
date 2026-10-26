// Exercise the real shell helper with mocked MongoDB commands; no server needed.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname + '/../ansible/files/cluster.js', 'utf8');
const names = [1,2,3].map(i => `mongo${i}.mongodb.home.arpa:27017`);
function run(phase, options = {}) {
  let initiated = 0, created = 0, writes = 0, remoteCalls = 0, sleeps = 0;
  const prints = [];
  const hello = options.hello || {setName:'rs0', isWritablePrimary:true, primary:names[0]};
  const admin = {
    auth() { return options.auth !== false; },
    createUser() { if (options.existingUser) throw Error('Unauthorized'); created++; },
    runCommand(cmd) {
      if(cmd.hello) return {ok:1, ...hello};
      if(cmd.replSetInitiate) { initiated++; assert.deepEqual(Array.from(cmd.replSetInitiate.members, m=>m.host), names); return {ok:1}; }
      if(cmd.replSetGetConfig) return {ok:1, config:{members:names.map(()=>({votes:1}))}};
      if(cmd.replSetGetStatus) return {ok:1, members:names.map((name,i)=>({name,health:1,state:i?2:1,stateStr:i?'SECONDARY':'PRIMARY'}))};
      throw Error('Unexpected admin command');
    }
  };
  const lab = {
    runCommand(cmd) { writes++; assert.equal(cmd.writeConcern.w,3); return options.writeError ? {ok:1, writeConcernError:{errmsg:'timed out'}} : {ok:1}; },
    replication_probe: {findOne() { return {token:options.stale ? 'old' : 'build-1'}; }}
  };
  const context = {process:{env:{LAB_PHASE:phase,LAB_TOKEN:'build-1',MONGO_ADMIN_USER:'admin',MONGO_ADMIN_PASSWORD:'secret'}},
    db:{getSiblingDB:n=>n==='admin'?admin:lab, getMongo:()=>({setReadPref(){}})},
    Mongo: function(uri) {
      assert.match(uri, /directConnection=true/);
      const host = uri.split('mongodb://')[1].split('/')[0];
      const index = names.indexOf(host);
      assert.ok(index >= 0);
      return {getDB() {return {runCommand() {
        const round = Math.floor(remoteCalls++ / 3);
        if (options.remoteError) throw Error('member unreachable');
        const chosen = options.writerIndex === undefined ? 0 : options.writerIndex;
        const h = {ok:1, setName:'rs0', me:host, primary:names[chosen],
          isWritablePrimary:index===chosen, secondary:index!==chosen};
        if (options.announcedBeforeWritable && round===0) {
          h.isWritablePrimary=false; h.secondary=index!==chosen;
        }
        if (options.twoWriters) h.isWritablePrimary=index<2;
        if (options.disagreement && round===0 && index===2) h.primary=names[2];
        return h;
      }}}};
    },
    print(value){prints.push(value);},sleep(){sleeps++;},quit(code){throw Error('EXIT '+code);} };
  vm.runInNewContext(source, context);
  return {initiated,created,writes,prints,sleeps};
}
assert.equal(run('probe').initiated,0);
assert.equal(run('initiate').initiated,1);
assert.equal(run('user').created,0);
assert.throws(()=>run('user',{auth:false,existingUser:true}),/Unauthorized/);
assert.equal(run('write').writes,1);
assert.throws(()=>run('write',{writeError:true}),/timed out/);
assert.throws(()=>run('read',{stale:true}),/not replicated/);
run('read');
const immediate = run('discover-primary');
assert.equal(JSON.parse(immediate.prints[0]).primary_node, 'mongo1');
const delayed = run('discover-primary', {announcedBeforeWritable:true});
assert.equal(delayed.sleeps,1);
assert.equal(JSON.parse(delayed.prints[0]).primary_node,'mongo1');
const different = run('discover-primary', {writerIndex:2});
assert.equal(JSON.parse(different.prints[0]).primary_node,'mongo3');
assert.equal(run('discover-primary',{disagreement:true}).sleeps,1);
assert.throws(()=>run('discover-primary',{twoWriters:true}),/No consistent writable primary/);
assert.throws(()=>run('discover-primary',{remoteError:true}),/member unreachable/);
console.log('14 MongoDB helper checks passed, including election readiness and diagnostics');
