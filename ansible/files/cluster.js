// Secrets come from the remote process environment, never command arguments.
const admin = db.getSiblingDB('admin');
const phase = process.env.LAB_PHASE;
const hosts = ['mongo1', 'mongo2', 'mongo3'].map(n => n + '.mongodb.home.arpa:27017');
function requireOK(r) { if (!r.ok || r.writeConcernError || (r.writeErrors && r.writeErrors.length)) throw new Error(JSON.stringify(r)); return r; }
function auth() {
  if (!admin.auth(process.env.MONGO_ADMIN_USER, process.env.MONGO_ADMIN_PASSWORD)) {
    throw new Error('Admin authentication failed; verify the original Jenkins credential');
  }
}
const hello = requireOK(admin.runCommand({hello: 1}));
if (phase === 'ready') {
  // Fresh members have no setName. Existing members must be fully online.
  if (hello.setName && !hello.isWritablePrimary && !hello.secondary) quit(1);
  quit(0);
}
if (phase === 'probe') {
  print(hello.setName ? 'INITIALIZED' : 'NEW');
} else if (phase === 'initiate') {
  requireOK(admin.runCommand({replSetInitiate: {
    _id: 'rs0', members: hosts.map((host, i) => ({_id: i, host}))
  }}));
} else if (phase === 'primary') {
  let elected = hello;
  for (let attempt = 0; !elected.primary && attempt < 60; attempt++) {
    sleep(2000); elected = requireOK(admin.runCommand({hello: 1}));
  }
  if (!elected.isWritablePrimary) quit(1);
} else if (phase === 'user') {
  // This is only executed locally on the elected primary. The localhost
  // exception allows first-user creation; wrong existing secrets fail closed.
  let authenticated = false;
  try { authenticated = !!admin.auth(process.env.MONGO_ADMIN_USER, process.env.MONGO_ADMIN_PASSWORD); }
  catch (e) { if (e.code !== 18) throw e; }
  if (!authenticated) {
    admin.createUser({user: process.env.MONGO_ADMIN_USER,
      pwd: process.env.MONGO_ADMIN_PASSWORD, roles: [{role: 'root', db: 'admin'}]},
      {w: 'majority', wtimeout: 60000});
    auth();
    print('CREATED');
  } else print('EXISTS');
} else if (phase === 'write') {
  auth();
  const status = requireOK(admin.runCommand({replSetGetStatus: 1}));
  if (status.members.length !== 3 || status.members.some(m => !hosts.includes(m.name)) ||
      status.members.filter(m => m.state === 1).length !== 1 ||
      status.members.filter(m => m.state === 2).length !== 2 ||
      status.members.some(m => m.health !== 1)) throw new Error('Replica set is not healthy');
  const cfg = requireOK(admin.runCommand({replSetGetConfig: 1})).config;
  if (cfg.members.some(m => m.arbiterOnly || m.votes !== 1)) throw new Error('Expected three voting data members');
  const lab = db.getSiblingDB('lab_health');
  requireOK(lab.runCommand({update: 'replication_probe', updates: [{q: {_id: 'jenkins'},
    u: {$set: {token: process.env.LAB_TOKEN, checkedAt: new Date()}}, upsert: true}],
    writeConcern: {w: 3, wtimeout: 60000}}));
  print(JSON.stringify(status.members.map(m => ({name: m.name, state: m.stateStr, health: m.health}))));
} else if (phase === 'read') {
  auth();
  db.getMongo().setReadPref('secondaryPreferred');
  const doc = db.getSiblingDB('lab_health').replication_probe.findOne({_id: 'jenkins'});
  if (!doc || doc.token !== process.env.LAB_TOKEN) throw new Error('Probe has not replicated locally');
  print('Replication verified');
} else throw new Error('Unknown phase');
