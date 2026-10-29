const body = document.getElementById("readiness-body");
const message = document.getElementById("readiness-message");
const detail = document.getElementById("readiness-detail");
const parking = document.getElementById("readiness-detail-parking");
const detailTitle = document.getElementById("readiness-detail-title");
const jobSummary = document.getElementById("readiness-job-summary");
const records = document.getElementById("readiness-records");
const filter = document.getElementById("readiness-filter");
let participants = [];
let selectedJob = null;
let selectedData = null;
let sortState = {key: "participant_code", direction: 1};

function esc(value) {
    return String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

async function api(url) {
    const response = await fetch(url, {credentials: "same-origin", cache: "no-store"});
    if (response.status === 401) {
        location.href = new URL("login", location.href).pathname;
        throw new Error("Admin session expired.");
    }
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Request failed.");
    return data;
}

function sorted(items) {
    const value = participant => {
        const job = participant.latest_job;
        if (sortState.key === "status") return job?.status || "not generated";
        if (sortState.key === "progress") return job?.percent ?? -1;
        if (sortState.key === "generated") return job?.finished_at || job?.created_at || "";
        return participant[sortState.key];
    };
    const numeric = new Set(["recordings", "progress"]);
    const result = [...items].sort((a, b) => {
        const av = value(a), bv = value(b);
        const comparison = numeric.has(sortState.key)
            ? Number(av || 0) - Number(bv || 0)
            : String(av ?? "").localeCompare(String(bv ?? ""), undefined, {numeric: true});
        return comparison * sortState.direction || a.participant_code.localeCompare(b.participant_code, undefined, {numeric: true});
    });
    document.querySelectorAll(".readiness-sort").forEach(button => {
        const active = button.dataset.sort === sortState.key;
        button.closest("th").setAttribute("aria-sort", active ? (sortState.direction === 1 ? "ascending" : "descending") : "none");
        button.querySelector("span").textContent = active ? (sortState.direction === 1 ? "▲" : "▼") : "↕";
    });
    return result;
}

function progress(job) {
    if (!job) return "—";
    return `<div class="readiness-progress"><span style="width:${Math.min(100, job.percent)}%"></span></div><small>${job.completed_files} / ${job.total_files} (${job.percent}%)</small>`;
}

function attachDetail(row) {
    body.querySelector(".readiness-detail-row")?.remove();
    const detailRow = document.createElement("tr");
    detailRow.className = "readiness-detail-row";
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.appendChild(detail);
    detailRow.appendChild(cell);
    row.after(detailRow);
    detail.classList.remove("hidden");
}

function hideDetail() {
    parking.appendChild(detail);
    detail.classList.add("hidden");
    body.querySelector(".readiness-detail-row")?.remove();
    selectedJob = null;
    selectedData = null;
    body.querySelectorAll(".readiness-view").forEach(button => { button.textContent = "View"; });
    history.replaceState(null, "", location.pathname);
}

async function loadParticipants() {
    try {
        const data = await api("/api/admin/readiness/participants");
        participants = data.participants;
        parking.appendChild(detail);
        body.replaceChildren();
        for (const participant of sorted(participants)) {
            const job = participant.latest_job;
            const row = document.createElement("tr");
            row.dataset.jobId = job?.id || "";
            row.innerHTML = `<td><strong>${esc(participant.participant_code)}</strong></td>
                <td>${participant.recordings}</td>
                <td>${job ? `<span class="readiness-status ${esc(job.status)}">${esc(job.status.replaceAll("_", " "))}</span>` : "Not generated"}</td>
                <td>${progress(job)}</td>
                <td>${esc(job?.finished_at || job?.created_at || "—")}</td>
                <td>${job ? `<button type="button" class="readiness-view">${selectedJob === job.id ? "Hide" : "View"}</button>` : "—"}</td>`;
            row.querySelector(".readiness-view")?.addEventListener("click", () => toggleJob(job.id, row));
            body.appendChild(row);
        }
        if (!participants.length) body.innerHTML = '<tr><td colspan="6">No participants found.</td></tr>';
        message.textContent = `${participants.filter(item => item.latest_job?.status === "completed").length} participants have a completed readiness export.`;
        if (selectedJob) {
            const row = Array.from(body.querySelectorAll("tr[data-job-id]")).find(item => item.dataset.jobId === selectedJob);
            if (row) attachDetail(row); else hideDetail();
        }
    } catch (error) { message.textContent = error.message; }
}

function mediaUrl(jobId, sentenceId, kind) {
    return `/api/admin/readiness/jobs/${encodeURIComponent(jobId)}/files/${encodeURIComponent(sentenceId)}/${kind}`;
}

function loadMedia(panel, item) {
    if (panel.dataset.loaded === "true" || item.status !== "complete") return;
    panel.dataset.loaded = "true";
    const target = panel.querySelector(".readiness-record-body");
    target.insertAdjacentHTML("beforeend", `<div class="readiness-media-grid">
        <div class="readiness-media-card"><h4>Full-frame video</h4><video controls preload="metadata" src="${mediaUrl(selectedJob, item.sentence_id, "video")}"></video></div>
        <div class="readiness-media-card"><h4>Face video</h4><video controls preload="metadata" src="${mediaUrl(selectedJob, item.sentence_id, "face")}"></video></div>
        <div class="readiness-media-card"><h4>Mouth video</h4><video controls preload="metadata" src="${mediaUrl(selectedJob, item.sentence_id, "mouth")}"></video></div>
        <div class="readiness-media-card"><h4>Audio</h4><audio controls preload="none" src="${mediaUrl(selectedJob, item.sentence_id, "audio")}"></audio></div>
    </div>`);
}

function renderRecords() {
    if (!selectedData) return;
    const query = filter.value.trim().toLocaleLowerCase();
    records.replaceChildren();
    for (const item of selectedData.results.filter(result => !query || `${result.sentence_id} ${result.reference_text}`.toLocaleLowerCase().includes(query))) {
        const panel = document.createElement("details");
        panel.className = "readiness-record";
        panel.innerHTML = `<summary>${esc(item.sentence_id)} <span class="readiness-record-status ${esc(item.status)}">${esc(item.status)}</span></summary>
            <div class="readiness-record-body">
                <p class="readiness-reference"><strong>Reference text</strong><br>${esc(item.reference_text || "Unavailable")}</p>
                ${item.error ? `<p class="readiness-error">${esc(item.error)}</p>` : ""}
            </div>`;
        panel.addEventListener("toggle", () => { if (panel.open) loadMedia(panel, item); });
        records.appendChild(panel);
    }
    if (!records.children.length) records.innerHTML = "<p>No matching recordings.</p>";
}

async function toggleJob(jobId, row) {
    if (selectedJob === jobId && !detail.classList.contains("hidden")) { hideDetail(); return; }
    selectedJob = jobId;
    body.querySelectorAll(".readiness-view").forEach(button => { button.textContent = "View"; });
    row.querySelector(".readiness-view").textContent = "Hide";
    attachDetail(row);
    try {
        selectedData = await api(`/api/admin/readiness/jobs/${encodeURIComponent(jobId)}`);
        detailTitle.textContent = `${selectedData.participant_code} Dataset Readiness`;
        jobSummary.innerHTML = `<div class="readiness-summary">
            <div><strong>Status</strong><br>${esc(selectedData.status.replaceAll("_", " "))}</div>
            <div><strong>Progress</strong><br>${selectedData.completed_files} / ${selectedData.total_files}</div>
            <div><strong>Failed</strong><br>${selectedData.failed_files}</div>
            <div><strong>Mode</strong><br>${esc(selectedData.export_mode)}</div>
            <div><strong>Generated</strong><br>${esc(selectedData.finished_at || selectedData.created_at || "—")}</div>
        </div>${selectedData.error ? `<p class="readiness-error">${esc(selectedData.error)}</p>` : ""}`;
        filter.value = "";
        renderRecords();
        history.replaceState(null, "", `?job=${encodeURIComponent(jobId)}`);
    } catch (error) { message.textContent = error.message; }
}

filter.addEventListener("input", renderRecords);
document.getElementById("readiness-refresh").addEventListener("click", loadParticipants);
document.querySelectorAll(".readiness-sort").forEach(button => button.addEventListener("click", () => {
    const key = button.dataset.sort;
    sortState = {key, direction: sortState.key === key ? -sortState.direction : 1};
    loadParticipants();
}));
window.addEventListener("load", async () => {
    const requestedJob = new URLSearchParams(location.search).get("job");
    selectedJob = null;
    await loadParticipants();
    if (requestedJob) {
        const row = Array.from(body.querySelectorAll("tr[data-job-id]")).find(item => item.dataset.jobId === requestedJob);
        if (row) await toggleJob(requestedJob, row);
    }
});
