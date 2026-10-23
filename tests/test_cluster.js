// Exercise the real shell helper with mocked MongoDB commands; no server needed.
const vm = require('node:vm');
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(__dirname + '/../ansible/files/cluster.js', 'utf8');
const names = [1,2,3].map(i => `mongo${i}.mongodb.home.arpa:27017`);
function run(phase, options = {}) {
  let initiated = 0, created = 0, writes = 0;
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
    print(){},sleep(){},quit(code){throw Error('EXIT '+code);} };
  vm.runInNewContext(source, context);
  return {initiated,created,writes};
}
assert.equal(run('probe').initiated,0);
assert.equal(run('initiate').initiated,1);
assert.equal(run('user').created,0);
assert.throws(()=>run('user',{auth:false,existingUser:true}),/Unauthorized/);
assert.equal(run('write').writes,1);
assert.throws(()=>run('write',{writeError:true}),/timed out/);
assert.throws(()=>run('read',{stale:true}),/not replicated/);
run('read');
console.log('8 MongoDB helper checks passed');
