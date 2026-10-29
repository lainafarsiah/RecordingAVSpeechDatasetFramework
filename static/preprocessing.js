const participantBody = document.getElementById("preprocessing-body");
const pageMessage = document.getElementById("page-message");
const refreshButton = document.getElementById("refresh-btn");
const jobDetail = document.getElementById("job-detail");
const detailParking = document.getElementById("job-detail-parking");
const jobSummary = document.getElementById("job-summary");
const resultBody = document.getElementById("result-body");

let participants = [];
let selectedJobId = null;
let pollTimer = null;
const launchingParticipants = new Set();
let preprocessingSort = {key: "participant_code", direction: 1};

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
}

function statusLabel(status) {
    return (status || "not_started").replaceAll("_", " ");
}

function progressMarkup(job) {
    if (!job) return "—";
    return `<div class="preprocess-progress" aria-label="${job.percent}% complete">
        <span style="width:${Math.min(100, job.percent)}%"></span></div>
        <small>${job.completed_files + job.failed_files} / ${job.total_files} (${job.percent}%)</small>`;
}

function sortedPreprocessingParticipants(items) {
    const {key, direction} = preprocessingSort;
    const numeric = new Set(["recordings", "progress", "wer"]);
    const value = participant => {
        const job = participant.latest_job;
        if (key === "status") return job?.status || "not started";
        if (key === "progress") return job?.percent ?? -1;
        if (key === "wer") return job?.summary?.overall_wer ?? -1;
        if (key === "generated") return job?.finished_at || job?.created_at || "";
        return participant[key];
    };
    const sorted = [...items].sort((left, right) => {
        const leftValue = value(left), rightValue = value(right);
        const comparison = numeric.has(key)
            ? Number(leftValue || 0) - Number(rightValue || 0)
            : String(leftValue ?? "").localeCompare(String(rightValue ?? ""), undefined, {numeric: true});
        return comparison * direction || left.participant_code.localeCompare(right.participant_code, undefined, {numeric: true});
    });
    document.querySelectorAll(".preprocess-sort-btn").forEach(button => {
        const active = button.dataset.sort === key;
        button.closest("th").setAttribute("aria-sort", active ? (direction === 1 ? "ascending" : "descending") : "none");
        button.querySelector(".sort-indicator").textContent = active ? (direction === 1 ? " ▲" : " ▼") : " ↕";
    });
    return sorted;
}

async function fetchJson(url, options) {
    const response = await fetch(url, {...options, cache: "no-store"});
    let data;
    try { data = await response.json(); } catch { data = {}; }
    if (response.status === 401) {
        window.location.href = new URL("login", window.location.href).pathname;
        throw new Error("Admin session expired.");
    }
    if (!response.ok) throw new Error(data.detail || "Request failed.");
    return data;
}

function attachDetail(row) {
    participantBody.querySelector(".preprocess-detail-row")?.remove();
    const detailRow = document.createElement("tr");
    detailRow.className = "preprocess-detail-row";
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.appendChild(jobDetail);
    detailRow.appendChild(cell);
    row.after(detailRow);
    jobDetail.classList.remove("hidden");
}

function hideJob() {
    detailParking.appendChild(jobDetail);
    jobDetail.classList.add("hidden");
    participantBody.querySelector(".preprocess-detail-row")?.remove();
    selectedJobId = null;
    history.replaceState(null, "", location.pathname);
    participantBody.querySelectorAll(".view-preprocessing").forEach(button => {
        button.textContent = "View";
    });
}

function renderParticipants() {
    detailParking.appendChild(jobDetail);
    participantBody.replaceChildren();
    if (!participants.length) {
        participantBody.innerHTML = '<tr><td colspan="7">No registered participants found.</td></tr>';
        return;
    }
    for (const participant of sortedPreprocessingParticipants(participants)) {
        const job = participant.latest_job;
        if (job && ["running", "queued"].includes(job.status)) {
            launchingParticipants.delete(participant.participant_code);
        }
        const active = launchingParticipants.has(participant.participant_code) ||
            ["running", "queued"].includes(job?.status);
        const row = document.createElement("tr");
        row.className = "preprocess-participant-row";
        row.dataset.participantCode = participant.participant_code;
        row.dataset.jobId = job?.job_id || "";
        row.innerHTML = `<td><strong>${escapeHtml(participant.participant_code)}</strong></td>
            <td>${participant.recordings}</td>
            <td><span class="preprocess-status ${escapeHtml(job?.status)}">${escapeHtml(active && !job ? "starting" : statusLabel(job?.status))}</span></td>
            <td>${progressMarkup(job)}</td>
            <td>${job?.summary?.overall_wer ?? "—"} / ${job?.summary?.overall_cer ?? "—"}<br>
                <small>${job?.summary?.scored_files ?? 0} scored recording(s)</small>
                ${job?.error ? `<p class="preprocess-error">${escapeHtml(job.error)}</p>` : ""}</td>
            <td>${escapeHtml(job?.finished_at || job?.created_at || "—")}</td>
            <td><div class="preprocess-actions">
                <button type="button" class="generate-preprocessing" ${active ? "disabled" : ""}>${active ? "Running…" : "Generate"}</button>
                ${job ? `<button type="button" class="view-preprocessing">${selectedJobId === job.job_id ? "Hide" : "View"}</button>` : ""}
            </div></td>`;
        row.querySelector(".generate-preprocessing").addEventListener("click", () => startPreprocessing(participant.participant_code));
        row.querySelector(".view-preprocessing")?.addEventListener("click", () => toggleJob(participant.participant_code, job.job_id, row));
        participantBody.appendChild(row);
    }
    if (selectedJobId) {
        const selectedRow = Array.from(participantBody.querySelectorAll(".preprocess-participant-row"))
            .find(row => row.dataset.jobId === selectedJobId);
        if (selectedRow) attachDetail(selectedRow);
    }
}

async function loadParticipants(silent = false) {
    if (silent && Array.from(jobDetail.querySelectorAll("video, audio")).some(media => !media.paused)) {
        schedule(2000);
        return;
    }
    try {
        const data = await fetchJson("/api/admin/preprocessing/participants");
        participants = data.participants;
        renderParticipants();
        if (!silent) pageMessage.textContent = `Results directory: ${data.output_root}`;
        const active = launchingParticipants.size > 0 || participants.some(item =>
            ["queued", "running"].includes(item.latest_job?.status));
        schedule(active ? 2000 : 0);
    } catch (error) {
        pageMessage.textContent = error.message;
        schedule(5000);
    }
}

async function startPreprocessing(code) {
    if (!confirm(`Generate preprocessing results for ${code}?`)) return;
    launchingParticipants.add(code);
    renderParticipants();
    try {
        await fetchJson(`/api/admin/preprocessing/participant/${encodeURIComponent(code)}/start`, {method: "POST"});
        pageMessage.textContent = `Preprocessing started for ${code}.`;
        schedule(500);
    } catch (error) {
        launchingParticipants.delete(code);
        pageMessage.textContent = error.message;
        renderParticipants();
    }
}

function renderResults(job) {
    jobDetail.classList.remove("hidden");
    jobSummary.innerHTML = `<p><strong>Participant:</strong> ${escapeHtml(job.participant_code)}</p>
        <p><strong>Status:</strong> ${escapeHtml(statusLabel(job.status))}</p>
        <p><strong>Progress:</strong> ${job.completed_files + job.failed_files} / ${job.total_files};
           <strong>Flagged:</strong> ${job.flagged_files}; <strong>Failed:</strong> ${job.failed_files}</p>
        <p><strong>Overall WER:</strong> ${job.summary?.overall_wer ?? "—"};
           <strong>Overall CER:</strong> ${job.summary?.overall_cer ?? "—"};
           <strong>Scored recordings:</strong> ${job.summary?.scored_files ?? 0}</p>
        <p><strong>Stage:</strong> ${escapeHtml(job.stage)} ${escapeHtml(job.current_sentence)}</p>
        ${job.error ? `<p class="preprocess-error">${escapeHtml(job.error)}</p>` : ""}`;
    resultBody.replaceChildren();
    if (!job.results?.length) {
        resultBody.innerHTML = '<tr><td colspan="5">No completed files yet.</td></tr>';
        return;
    }
    for (const result of job.results) {
        const flags = [...new Set([...(result.quality?.flags || []), ...(result.timing?.flags || [])])];
        const row = document.createElement("tr");
        row.innerHTML = `<td><strong>${escapeHtml(result.sentence_id)}</strong></td>
            <td class="${["flagged", "review"].includes(result.status) ? "preprocess-result-flag" : ""}">${escapeHtml(result.status)}
                ${result.error ? `<p class="preprocess-error">${escapeHtml(result.error)}</p>` : ""}</td>
            <td><p><strong>Expected:</strong> ${escapeHtml(result.expected_text)}</p>
                <p><strong>Whisper:</strong> ${escapeHtml(result.transcript)}</p>
                <small>WER: ${result.wer ?? "—"}; CER: ${result.cer ?? "—"};
                    transcript: ${escapeHtml(result.quality?.transcript_decision || "—")}</small></td>
            <td>${flags.length ? escapeHtml(flags.join(", ")) : "No automatic flags"}<br>
                <small>Clean RMS: ${result.quality?.clean_audio?.rms_dbfs ?? "—"} dBFS;<br>
                silence: ${result.quality?.clean_audio?.silence_ratio ?? "—"}</small></td>
            <td><div class="video-comparison"><div><strong>Before cleaning</strong>
                ${result.source_video ? `<video controls preload="metadata" src="/api/admin/preprocessing/jobs/${job.job_id}/files/${encodeURIComponent(result.sentence_id)}/original"></video>` : "Unavailable"}</div>
                <div><strong>After cleaning</strong>
                ${result.clean_video ? `<video controls preload="metadata" src="/api/admin/preprocessing/jobs/${job.job_id}/files/${encodeURIComponent(result.sentence_id)}/video"></video>` : "Unavailable"}</div></div>
                ${result.clean_audio ? `<audio controls preload="none" src="/api/admin/preprocessing/jobs/${job.job_id}/files/${encodeURIComponent(result.sentence_id)}/audio"></audio><br>` : ""}
                ${result.silent_video ? `<a href="/api/admin/preprocessing/jobs/${job.job_id}/files/${encodeURIComponent(result.sentence_id)}/silent-video" target="_blank">Silent video</a>` : ""}</td>`;
        resultBody.appendChild(row);
    }
}

async function showJob(jobId) {
    try {
        renderResults(await fetchJson(`/api/admin/preprocessing/jobs/${encodeURIComponent(jobId)}`));
    } catch (error) { pageMessage.textContent = error.message; }
}

async function toggleJob(code, jobId, row) {
    if (selectedJobId === jobId && !jobDetail.classList.contains("hidden")) {
        hideJob();
        return;
    }
    selectedJobId = jobId;
    participantBody.querySelectorAll(".view-preprocessing").forEach(button => { button.textContent = "View"; });
    row.querySelector(".view-preprocessing").textContent = "Hide";
    attachDetail(row);
    history.replaceState(null, "", `?participant=${encodeURIComponent(code)}&job=${encodeURIComponent(jobId)}`);
    await showJob(jobId);
}

function schedule(delay) {
    clearTimeout(pollTimer);
    if (!delay) return;
    pollTimer = setTimeout(() => loadParticipants(true), delay);
}

refreshButton.addEventListener("click", async () => {
    await loadParticipants();
    if (selectedJobId) await showJob(selectedJobId);
});
document.querySelectorAll(".preprocess-sort-btn").forEach(button => {
    button.addEventListener("click", () => {
        const key = button.dataset.sort;
        preprocessingSort = {
            key,
            direction: preprocessingSort.key === key ? -preprocessingSort.direction : 1,
        };
        renderParticipants();
    });
});
window.addEventListener("load", async () => {
    const params = new URLSearchParams(location.search);
    selectedJobId = params.get("job");
    await loadParticipants();
    if (selectedJobId) await showJob(selectedJobId);
});
