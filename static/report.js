const participantBody = document.getElementById("report-participants");
const message = document.getElementById("report-message");
const detail = document.getElementById("report-detail");
const title = document.getElementById("report-title");
const statusBadge = document.getElementById("report-status");
const progress = document.getElementById("report-progress");
const summary = document.getElementById("report-summary");
const thresholds = document.getElementById("report-thresholds");
const records = document.getElementById("report-records");
const rawJson = document.getElementById("report-json");
const detailParking = document.getElementById("report-detail-parking");
let selectedRun = new URLSearchParams(location.search).get("run");
let selectedParticipant = new URLSearchParams(location.search).get("participant");
let pollTimer;
let reportSort = {key: "participant_code", direction: 1};

function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[char]);
}

async function api(url, options) {
    const response = await fetch(url, {...options, cache: "no-store"});
    let data = {};
    try { data = await response.json(); } catch {}
    if (response.status === 401) {
        location.href = new URL("login", location.href).pathname;
        throw new Error("Admin session expired.");
    }
    if (!response.ok) throw new Error(data.detail || "Request failed.");
    return data;
}

function progressBar(run) {
    if (!run) return "—";
    return `<div class="report-progress-track"><div class="report-progress-fill" style="width:${Math.min(100, run.percent)}%"></div></div>
        <small>${run.completed_recordings} / ${run.total_recordings} (${run.percent}%)</small>`;
}

function sortedReportParticipants(items) {
    const {key, direction} = reportSort;
    const numeric = new Set(["recordings", "progress", "wer"]);
    const value = participant => {
        const run = participant.latest_run;
        if (key === "status") return run?.status || "not generated";
        if (key === "progress") return run?.percent ?? -1;
        if (key === "wer") return run?.overall_wer ?? -1;
        if (key === "generated") return run?.finished_at || run?.created_at || "";
        return participant[key];
    };
    const sorted = [...items].sort((left, right) => {
        const leftValue = value(left), rightValue = value(right);
        const comparison = numeric.has(key)
            ? Number(leftValue || 0) - Number(rightValue || 0)
            : String(leftValue ?? "").localeCompare(String(rightValue ?? ""), undefined, {numeric: true});
        return comparison * direction || left.participant_code.localeCompare(right.participant_code, undefined, {numeric: true});
    });
    document.querySelectorAll(".report-sort-btn").forEach(button => {
        const active = button.dataset.sort === key;
        button.closest("th").setAttribute("aria-sort", active ? (direction === 1 ? "ascending" : "descending") : "none");
        button.querySelector(".sort-indicator").textContent = active ? (direction === 1 ? " ▲" : " ▼") : " ↕";
    });
    return sorted;
}

async function loadParticipants(silent = false) {
    try {
        const data = await api("/api/admin/evaluations/participants");
        detailParking.appendChild(detail);
        participantBody.replaceChildren();
        for (const participant of sortedReportParticipants(data.participants)) {
            const run = participant.latest_run;
            const row = document.createElement("tr");
            row.className = "report-participant-row";
            row.dataset.participantCode = participant.participant_code;
            row.dataset.runId = run?.id || "";
            row.innerHTML = `<td><strong>${esc(participant.participant_code)}</strong></td>
                <td>${participant.recordings}</td>
                <td>${run ? `<span class="report-status ${esc(run.status)}">${esc(run.status)}</span>` : "Not generated"}</td>
                <td>${progressBar(run)}</td>
                <td>${run?.overall_wer ?? "—"} / ${run?.overall_cer ?? "—"}</td>
                <td>${esc(run?.finished_at || run?.created_at || "—")}</td>
                <td><button class="generate-report" ${run?.status === "running" ? "disabled" : ""}>Generate</button>
                ${run ? `<button class="view-report">${selectedRun === run.id ? "Hide" : "View"}</button>` : ""}</td>`;
            row.querySelector(".generate-report").addEventListener("click", () => startRun(participant.participant_code));
            row.querySelector(".view-report")?.addEventListener("click", () => toggleRun(participant.participant_code, run.id, row));
            participantBody.appendChild(row);
        }
        if (!data.participants.length) participantBody.innerHTML = '<tr><td colspan="7">No participants found.</td></tr>';
        if (selectedRun) {
            const selectedRow = Array.from(participantBody.querySelectorAll(".report-participant-row"))
                .find(row => row.dataset.runId === selectedRun);
            if (selectedRow) attachDetail(selectedRow);
        }
        if (!silent) message.textContent = "Reports are generated from each participant’s current recordings.";
        const active = data.participants.some(item => item.latest_run?.status === "running");
        schedule(active ? 2000 : 10000);
    } catch (error) {
        message.textContent = error.message;
        schedule(10000);
    }
}

async function startRun(code) {
    if (!confirm(`Generate a new evaluation report for ${code}?`)) return;
    try {
        const data = await api(`/api/admin/evaluations/participant/${encodeURIComponent(code)}`, {method: "POST"});
        selectedRun = data.run.id;
        selectedParticipant = code;
        history.replaceState(null, "", `?participant=${encodeURIComponent(code)}&run=${encodeURIComponent(selectedRun)}`);
        await loadParticipants(true);
        await showRun(selectedRun, false);
    } catch (error) { message.textContent = error.message; }
}

function attachDetail(row) {
    participantBody.querySelector(".report-detail-row")?.remove();
    const detailRow = document.createElement("tr");
    detailRow.className = "report-detail-row";
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.appendChild(detail);
    detailRow.appendChild(cell);
    row.after(detailRow);
    detail.classList.remove("hidden");
}

function hideRun() {
    detailParking.appendChild(detail);
    detail.classList.add("hidden");
    participantBody.querySelector(".report-detail-row")?.remove();
    selectedRun = null;
    selectedParticipant = null;
    history.replaceState(null, "", location.pathname);
    participantBody.querySelectorAll(".view-report").forEach(button => { button.textContent = "View"; });
}

async function toggleRun(code, runId, row) {
    if (selectedRun === runId && !detail.classList.contains("hidden")) {
        hideRun();
        return;
    }
    selectedRun = runId;
    selectedParticipant = code;
    participantBody.querySelectorAll(".view-report").forEach(button => { button.textContent = "View"; });
    row.querySelector(".view-report").textContent = "Hide";
    attachDetail(row);
    history.replaceState(null, "", `?participant=${encodeURIComponent(code)}&run=${encodeURIComponent(runId)}`);
    await showRun(runId, false);
}

function metric(label, value) {
    return `<div class="report-metric"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`;
}

function normalizedWords(value) {
    const normalized = String(value || "").normalize("NFKC").toLocaleLowerCase()
        .replace(/[^\p{L}\p{N}_\s]/gu, " ").trim();
    return normalized ? normalized.split(/\s+/) : [];
}

function editDistance(left, right) {
    let previous = Array.from({length: right.length + 1}, (_, index) => index);
    for (let leftIndex = 1; leftIndex <= left.length; leftIndex += 1) {
        const current = [leftIndex];
        for (let rightIndex = 1; rightIndex <= right.length; rightIndex += 1) {
            current.push(Math.min(
                current[rightIndex - 1] + 1,
                previous[rightIndex] + 1,
                previous[rightIndex - 1] + (left[leftIndex - 1] === right[rightIndex - 1] ? 0 : 1),
            ));
        }
        previous = current;
    }
    return previous[right.length];
}

function transcriptMetrics(asr) {
    const reference = normalizedWords(asr.reference_text);
    const hypothesis = normalizedWords(asr.hypothesis_text);
    const referenceCharacters = Array.from(reference.join(""));
    const hypothesisCharacters = Array.from(hypothesis.join(""));
    const cer = asr.character_error_rate ??
        editDistance(referenceCharacters, hypothesisCharacters) / Math.max(1, referenceCharacters.length);
    const wer = asr.word_error_rate ??
        editDistance(reference, hypothesis) / Math.max(1, reference.length);
    const decision = asr.transcript_decision || (
        cer > 0.20 || (wer > 0.25 && cer > 0.10) ? "flag" : cer > 0.10 ? "review" : "pass"
    );
    return {
        cer,
        cerPercent: asr.character_error_rate_percent ?? Math.round(cer * 10000) / 100,
        wer,
        decision,
    };
}

function renderReport(run) {
    const expandedRecords = new Set(
        Array.from(records.querySelectorAll(".record-report[open]"), panel => panel.dataset.recordId)
    );
    const rawWasOpen = rawJson.closest("details").open;
    detail.classList.remove("hidden");
    title.textContent = `${run.participant_code} Evaluation Result`;
    statusBadge.className = `report-status ${run.status}`;
    statusBadge.textContent = run.status;
    progress.innerHTML = progressBar(run);
    if (run.error) progress.insertAdjacentHTML("beforeend", `<p class="preprocess-error">${esc(run.error)}</p>`);
    const report = run.report;
    summary.replaceChildren(); thresholds.replaceChildren(); records.replaceChildren(); rawJson.textContent = "";
    if (!report) {
        summary.innerHTML = '<p>The report will appear here when evaluation finishes.</p>';
        return;
    }
    const average = report.dataset_averages || {};
    const recordMetrics = (report.records || []).map(item => transcriptMetrics(item.asr_evaluation || {}));
    const averageCerPercent = average.average_character_error_rate_percent ?? (
        recordMetrics.length
            ? Math.round(recordMetrics.reduce((total, item) => total + item.cer, 0) / recordMetrics.length * 10000) / 100
            : "—"
    );
    summary.innerHTML = metric("Total recordings", report.total_recordings_processed) +
        metric("Passed", report.passed_count) +
        (report.review_count === undefined ? "" : metric("Review", report.review_count)) +
        metric("Flagged", report.flagged_count) +
        metric("Average WER", `${average.average_word_error_rate_percent ?? "—"}%`) +
        metric("Average CER", `${averageCerPercent}%`) +
        metric("Average brightness", average.average_brightness ?? "—");
    const limits = report.thresholds_applied || {};
    thresholds.innerHTML = `<h3>Thresholds</h3><ul class="report-threshold-list">${Object.entries(limits).map(([key,value]) => `<li><strong>${esc(key.replaceAll("_", " "))}:</strong> ${esc(value)}</li>`).join("")}</ul>`;
    for (const item of report.records || []) {
        const asr = item.asr_evaluation || {}, audio = item.audio_evaluation || {}, video = item.video_evaluation || {};
        const transcript = transcriptMetrics(asr);
        const panel = document.createElement("details");
        panel.className = "record-report";
        panel.dataset.recordId = item.sentence_id || item.recording_id;
        panel.open = expandedRecords.has(panel.dataset.recordId);
        panel.innerHTML = `<summary>${esc(item.sentence_id || item.recording_id)} <span class="record-status ${esc(item.status)}">${esc(item.status)}</span></summary>
            <div class="record-report-body">
                <p class="record-flags">${item.flags?.length ? esc(item.flags.join(", ")) : "No flags"}</p>
                <div class="record-metrics"><div><strong>WER</strong><br>${esc(asr.word_error_rate_percent ?? "—")}%</div>
                <div><strong>CER</strong><br>${esc(transcript.cerPercent)}%</div>
                <div><strong>Transcript decision</strong><br>${esc(transcript.decision.toUpperCase())}</div>
                <div><strong>Mean volume</strong><br>${esc(audio.mean_volume_db ?? "—")} dB</div>
                <div><strong>Max volume</strong><br>${esc(audio.max_volume_db ?? "—")} dB</div>
                <div><strong>Brightness</strong><br>${esc(video.avg_brightness ?? "—")} (${esc(video.exposure_status || "—")})</div></div>
                <div class="transcript-comparison"><div><strong>Reference</strong><p>${esc(asr.reference_text || "—")}</p></div>
                <div><strong>ASR hypothesis</strong><p>${esc(asr.hypothesis_text || "—")}</p></div></div>
            </div>`;
        records.appendChild(panel);
    }
    rawJson.textContent = JSON.stringify(report, null, 2);
    rawJson.closest("details").open = rawWasOpen;
}

async function showRun(runId, scroll = true) {
    selectedRun = runId;
    try {
        const run = await api(`/api/admin/evaluations/runs/${encodeURIComponent(runId)}`);
        selectedParticipant = run.participant_code;
        renderReport(run);
        if (scroll) detail.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { message.textContent = error.message; }
}

function schedule(delay) {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
        await loadParticipants(true);
        if (selectedRun) await showRun(selectedRun, false);
    }, delay);
}

document.getElementById("report-refresh").addEventListener("click", async () => {
    await loadParticipants();
    if (selectedRun) await showRun(selectedRun, false);
});
document.querySelectorAll(".report-sort-btn").forEach(button => {
    button.addEventListener("click", () => {
        const key = button.dataset.sort;
        reportSort = {key, direction: reportSort.key === key ? -reportSort.direction : 1};
        loadParticipants(true);
    });
});
window.addEventListener("load", async () => {
    await loadParticipants();
    if (selectedRun) await showRun(selectedRun, false);
});
