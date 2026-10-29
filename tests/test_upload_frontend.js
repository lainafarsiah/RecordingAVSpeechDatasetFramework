const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function setup(responses) {
    const elements = new Map();
    const calls = [];
    const context = vm.createContext({
        console,
        document: {getElementById(id) {
            if (!elements.has(id)) elements.set(id, {
                textContent: '', style: {}, addEventListener() {},
                classList: {add() {}, remove() {}, contains() { return false; }}
            });
            return elements.get(id);
        }},
        window: {addEventListener() {}},
        setTimeout(callback) { callback(); },
        FormData: class { append() {} },
        async fetch(url, options) {
            calls.push(url);
            assert.ok(responses.length, `Unexpected request: ${url}`);
            const response = responses.shift();
            return {ok: response.ok !== false, async json() { return response; }};
        }
    });
    vm.runInContext(fs.readFileSync('static/app.js', 'utf8'), context);
    return {context, calls, elements};
}

(async () => {
    const queued = setup([
        {job_id: 'job1'}, {status: 'queued'}, {status: 'processing'},
        {status: 'complete', result: {saved: true}}
    ]);
    const result = await queued.context.uploadRecording({}, {sentence_id: 'S1'});
    assert.equal(result.saved, true);
    assert.deepEqual(queued.calls, [
        '/api/recordings/upload', '/api/recordings/jobs/job1',
        '/api/recordings/jobs/job1', '/api/recordings/jobs/job1'
    ]);

    const resumed = setup([
        {pending_jobs: ['job2'], completed_sentence_ids: []},
        {status: 'complete', result: {}},
        {pending_jobs: [], completed_sentence_ids: ['S1']}
    ]);
    const session = await resumed.context.getParticipantSession('P0001');
    assert.equal(session.completed_sentence_ids[0], 'S1');
    assert.deepEqual(resumed.calls, [
        '/api/session/P0001', '/api/recordings/jobs/job2', '/api/session/P0001'
    ]);

    const failed = setup([{status: 'failed', error: 'Please retry'}]);
    await assert.rejects(failed.context.waitForRecordingJob('job3'), /Please retry/);
    const disconnected = setup([{ok: false}]);
    await assert.rejects(disconnected.context.waitForRecordingJob('job4'), /refresh to resume/);
    const full = setup([{ok: false, detail: 'Queue full'}]);
    await assert.rejects(full.context.uploadRecording({}, {sentence_id: 'S1'}), /Queue full/);
    console.log('PASS: frontend queue polling, completion, refresh/resume, failed jobs, and request errors.');
})().catch(error => { console.error(error); process.exitCode = 1; });
