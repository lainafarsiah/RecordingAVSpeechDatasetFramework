const speakingAbilityLabels = {A: "A. Poor (Kurang baik)", B: "B. Fair (Cukup)", C: "C. Good (Baik)", D: "D. Very good (Sangat baik)"};
const communicationStyleLabels = {A: "A. Very quiet (Sangat pendiam)", B: "B. Somewhat quiet (Cenderung pendiam)", C: "C. Somewhat talkative (Cukup suka berbicara)", D: "D. Very talkative (Sangat suka berbicara)"};

const totalParticipantsEl = document.getElementById("total-participants");
const totalRecordingsEl = document.getElementById("total-recordings");
const completedParticipantsEl = document.getElementById("completed-participants");

const participantsTableBody = document.getElementById("participants-table-body");
const dashboardPage = document.getElementById("dashboard-page");
const credentialsPage = document.getElementById("credentials-page");
const showDashboardBtn = document.getElementById("show-dashboard-btn");
const showCredentialsBtn = document.getElementById("show-credentials-btn");
const generateCredentialBtn = document.getElementById("generate-credential-btn");
const generatedCredentialBox = document.getElementById("generated-credential-box");
const credentialsTableBody = document.getElementById("credentials-table-body");

const participantDetailSection = document.getElementById("participant-detail-section");
const participantDetailInfo = document.getElementById("participant-detail-info");
const missingSentencesEl = document.getElementById("missing-sentences");
const recordingsTableBody = document.getElementById("recordings-table-body");

const fullRetakeBtn = document.getElementById("full-retake-btn");
const fullRetakeStatus = document.getElementById("full-retake-status");
const fullRetakeDownload = document.getElementById("full-retake-download");
const fullRetakesInProgress = new Set();
const selectAllRecordings = document.getElementById("select-all-recordings");
const bulkRetakeBtn = document.getElementById("bulk-retake-btn");
const bulkRetakeCount = document.getElementById("bulk-retake-count");
const bulkRetakeStatus = document.getElementById("bulk-retake-status");

function updateBulkRetakeSelection() {
    const available = Array.from(recordingsTableBody.querySelectorAll(".recording-retake-checkbox:not(:disabled)"));
    const selected = available.filter(checkbox => checkbox.checked);
    bulkRetakeCount.textContent = `${selected.length} selected`;
    bulkRetakeBtn.disabled = selected.length === 0;
    selectAllRecordings.disabled = available.length === 0;
    selectAllRecordings.checked = available.length > 0 && selected.length === available.length;
    selectAllRecordings.indeterminate = selected.length > 0 && selected.length < available.length;
}

selectAllRecordings.addEventListener("change", () => {
    recordingsTableBody.querySelectorAll(".recording-retake-checkbox:not(:disabled)").forEach(checkbox => {
        checkbox.checked = selectAllRecordings.checked;
    });
    updateBulkRetakeSelection();
});

bulkRetakeBtn.addEventListener("click", async () => {
    const selected = Array.from(recordingsTableBody.querySelectorAll(".recording-retake-checkbox:checked"));
    const sentenceIds = selected.map(checkbox => checkbox.dataset.sentenceId);
    const participantCode = currentParticipantCode;
    if (!participantCode || sentenceIds.length === 0) return;
    if (!confirm(
        `Request retake for ${sentenceIds.length} selected recording(s) from ${participantCode}? ` +
        "The participant will see these sentences again."
    )) return;

    bulkRetakeBtn.disabled = true;
    bulkRetakeStatus.textContent = "Applying selected retakes…";
    try {
        const response = await fetch("/api/admin/recordings/bulk-retake", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({participant_code: participantCode, sentence_ids: sentenceIds})
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Failed to request selected retakes.");
        await loadAdminSummary();
        if (currentParticipantCode === participantCode) bulkRetakeStatus.textContent = data.message;
    } catch (error) {
        bulkRetakeStatus.textContent = error.message;
        updateBulkRetakeSelection();
    }
});

fullRetakeBtn.addEventListener("click", async () => {
    const code = currentParticipantCode;
    if (!code || fullRetakesInProgress.has(code)) return;
    fullRetakesInProgress.add(code);
    fullRetakeBtn.disabled = true;
    fullRetakeStatus.textContent = "Backing up recordings and verifying the archive. Please wait...";
    try {
        const response = await fetch(`/api/admin/participant/${encodeURIComponent(code)}/full-retake`, {method: "POST"});
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || "Full retake could not be requested.");
        await loadAdminSummary();
        if (currentParticipantCode === code) {
            fullRetakeStatus.textContent = data.message;
            showFullRetakeBackup(data.backup);
        }
    } catch (error) {
        if (currentParticipantCode === code) fullRetakeStatus.textContent = `${error.message} If the connection was interrupted, refresh to check whether the backup completed.`;
    } finally {
        fullRetakesInProgress.delete(code);
        if (currentParticipantCode === code) fullRetakeBtn.disabled = false;
    }
});

function showFullRetakeBackup(backup) {
    fullRetakeDownload.classList.toggle("hidden", !backup);
    if (backup) fullRetakeDownload.href = backup.download_url;
    else fullRetakeDownload.removeAttribute("href");
}

const detailParking = document.getElementById("participant-detail-parking");
let currentParticipantCode = "";
let participantData = new Map();
let participantSort = { key: "participant_code", direction: 1 };
let detailRequest = 0;
let previewObserver = null;

function clearPreviews() {
    if (previewObserver) previewObserver.disconnect();
    previewObserver = null;
    recordingsTableBody.querySelectorAll("video").forEach(video => {
        video.pause();
        video.removeAttribute("src");
        video.load();
    });
}

function closeParticipantDetail() {
    detailRequest += 1;
    clearPreviews();
    detailParking.appendChild(participantDetailSection);
    participantDetailSection.classList.add("hidden");
    participantsTableBody.querySelector(".participant-detail-row")?.remove();
    participantsTableBody.querySelectorAll(".participant-view-btn").forEach(button => {
        button.textContent = "View";
        button.setAttribute("aria-expanded", "false");
    });
    currentParticipantCode = "";
}

const indonesianAccentLabels = {
    standard_neutral: "A. Indonesia baku/netral",
    jakarta_betawi: "B. Indonesia Jakarta/Betawi",
    java: "C. Indonesia Jawa",
    sunda: "D. Indonesia Sunda",
    medan_north_sumatra: "E. Indonesia Medan/Sumatra Utara",
    aceh: "F. Indonesia Aceh",
    minang: "G. Indonesia Minang",
    palembang_south_sumatra: "H. Indonesia Palembang/Sumatra Selatan",
    banjar_kalimantan: "I. Indonesia Banjar/Kalimantan",
    makassar_bugis: "J. Indonesia Makassar/Bugis",
    manado: "K. Indonesia Manado",
    bali: "L. Indonesia Bali",
    nusa_tenggara: "M. Indonesia Nusa Tenggara",
    ambon_maluku: "N. Indonesia Ambon/Maluku",
    papua: "O. Indonesia Papua",
    mixed: "P. Campuran",
    unknown_unsure: "Q. Tidak tahu/tidak yakin",
    other: "R. Lainnya"
};

function formatIndonesianAccent(value) {
    return indonesianAccentLabels[value] || value || "-";
}

function formatDuration(totalSeconds) {
    const safeSeconds = Math.max(0, Math.round(Number(totalSeconds) || 0));
    const minutes = Math.floor(safeSeconds / 60);
    const seconds = safeSeconds % 60;

    return `${minutes}:${String(seconds).padStart(2, "0")}`;
}

function showAdminPage(pageName, updateUrl = true) {
    const showCredentials = pageName === "credentials";

    dashboardPage.classList.toggle("hidden", showCredentials);
    credentialsPage.classList.toggle("hidden", !showCredentials);
    showDashboardBtn.classList.toggle("active", !showCredentials);
    showCredentialsBtn.classList.toggle("active", showCredentials);
    showDashboardBtn.setAttribute("aria-pressed", String(!showCredentials));
    showCredentialsBtn.setAttribute("aria-pressed", String(showCredentials));
    if (updateUrl) {
        const basePath = window.location.pathname.replace(/\/$/, "");
        const url = showCredentials ? `${basePath}?page=credentials` : basePath;
        history.replaceState(null, "", url);
    }

    if (showCredentials) {
        closeParticipantDetail();
        loadCredentials();
    } else {
        loadAdminSummary();
    }
}

async function loadCredentials() {
    credentialsTableBody.innerHTML = `
        <tr>
            <td colspan="4">Loading...</td>
        </tr>
    `;

    try {
        const response = await fetch("/api/admin/credentials");

        if (!response.ok) {
            throw new Error("Failed to load participant logins.");
        }

        const data = await response.json();
        renderCredentialsTable(data.credentials);

    } catch (error) {
        console.error(error);
        credentialsTableBody.innerHTML = `
            <tr>
                <td colspan="4">Error: ${error.message}</td>
            </tr>
        `;
    }
}

function renderCredentialsTable(credentials) {
    if (!credentials || credentials.length === 0) {
        credentialsTableBody.innerHTML = `
            <tr>
                <td colspan="4">No participant logins generated yet.</td>
            </tr>
        `;
        return;
    }

    credentialsTableBody.innerHTML = "";

    credentials.forEach(item => {
        const row = document.createElement("tr");
        row.innerHTML = `
            <td>${item.participant_code}</td>
            <td><code>${item.password}</code></td>
            <td>${item.created_at}</td>
            <td>${item.profile_complete ? "Yes" : "No"}</td>
        `;
        credentialsTableBody.appendChild(row);
    });
}

async function generateCredential() {
    generateCredentialBtn.disabled = true;
    generatedCredentialBox.classList.add("hidden");
    generatedCredentialBox.textContent = "";

    try {
        const response = await fetch("/api/admin/credentials/generate", {
            method: "POST"
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to generate participant login.");
        }

        const credential = await response.json();
        generatedCredentialBox.innerHTML = `
            <p><strong>Participant ID:</strong> ${credential.participant_code}</p>
            <p><strong>Password:</strong> <code>${credential.password}</code></p>
        `;
        generatedCredentialBox.classList.remove("hidden");
        await loadCredentials();

    } catch (error) {
        console.error(error);
        alert(error.message);

    } finally {
        generateCredentialBtn.disabled = false;
    }
}

async function loadAdminSummary() {
    const expandedCode = currentParticipantCode;
    closeParticipantDetail();
    participantsTableBody.innerHTML = `
        <tr>
            <td colspan="10">Loading...</td>
        </tr>
    `;

    try {
        const response = await fetch("/api/admin/summary", { cache: "no-store" });

        if (!response.ok) {
            throw new Error("Failed to load admin summary.");
        }

        const data = await response.json();

        totalParticipantsEl.textContent = data.total_participants;
        totalRecordingsEl.textContent = data.total_recordings;
        // Older running servers may not yet return the aggregate field.
        const completedCount = data.completed_participants ?? (data.participants || []).filter(participant => {
            const total = Number(participant.total ?? data.total_sentences);
            return total > 0 && Number(participant.completed) === total;
        }).length;
        completedParticipantsEl.textContent = String(completedCount);

        renderParticipantsTable(data.participants);
        if (expandedCode) await loadParticipantDetail(expandedCode);

    } catch (error) {
        console.error(error);
        participantsTableBody.innerHTML = `
            <tr>
                <td colspan="10">Error: ${error.message}</td>
            </tr>
        `;
    }
}


function renderParticipantsTable(participants) {
    participantData = new Map((participants || []).map(participant => [participant.participant_code, participant]));
    if (!participants || participants.length === 0) {
        participantsTableBody.innerHTML = `
            <tr>
                <td colspan="10">No participants found.</td>
            </tr>
        `;
        return;
    }

    participantsTableBody.innerHTML = "";

    participants.forEach(p => {
        const row = document.createElement("tr");
        row.dataset.participantCode = p.participant_code;

        row.innerHTML = `
            <td>${p.participant_code}</td>
            <td>${p.age_group}</td>
            <td>${p.gender}</td>
            <td>${formatIndonesianAccent(p.indonesian_accent)}</td>
            <td>${p.completed} / ${p.total}</td>
            <td>
                <div class="small-progress-bar">
                    <div class="small-progress-fill" style="width: ${p.percentage}%"></div>
                </div>
                ${p.percentage}%
            </td>
            <td>${formatDuration(p.total_duration_seconds)}</td>
            <td>${p.last_recording_time || "-"}</td>
            <td>
                <label class="participant-validation-label">
                    <input type="checkbox" class="participant-validation" ${p.admin_validated ? "checked" : ""}
                        aria-label="Validated by admin: ${escapeHtml(p.participant_code)}">
                    <span>${p.admin_validated ? "Validated" : "Not validated"}</span>
                </label>
                <small class="validation-status" role="status"></small>
            </td>
            <td>
                <button class="participant-view-btn" aria-expanded="false" aria-controls="participant-detail-section" onclick="loadParticipantDetail('${p.participant_code}', true)">
                    View
                </button>
            </td>
        `;

        row.querySelector(".participant-validation").addEventListener("change", event => {
            saveParticipantValidation(p, event.target);
        });
        participantsTableBody.appendChild(row);
    });
    sortParticipantRows();
}

function sortParticipantRows() {
    const {key, direction} = participantSort;
    const numeric = new Set(["completed", "percentage", "total_duration_seconds", "admin_validated"]);
    const value = participant => key === "indonesian_accent"
        ? formatIndonesianAccent(participant[key]) : participant[key];
    const rows = Array.from(participantsTableBody.children).filter(row => row.dataset.participantCode);
    rows.sort((a, b) => {
        const left = participantData.get(a.dataset.participantCode);
        const right = participantData.get(b.dataset.participantCode);
        let comparison;
        if (numeric.has(key)) comparison = Number(value(left) || 0) - Number(value(right) || 0);
        else comparison = String(value(left) || "").localeCompare(String(value(right) || ""), undefined, { numeric: true });
        return comparison * direction || left.participant_code.localeCompare(right.participant_code, undefined, {numeric: true});
    });
    const detailRow = participantsTableBody.querySelector(".participant-detail-row");
    rows.forEach(row => {
        participantsTableBody.appendChild(row);
        if (detailRow && row.dataset.participantCode === currentParticipantCode) participantsTableBody.appendChild(detailRow);
    });
    document.querySelectorAll(".participant-sort-btn").forEach(button => {
        const active = button.dataset.sort === key;
        button.closest("th").setAttribute("aria-sort", active ? (direction === 1 ? "ascending" : "descending") : "none");
        button.querySelector(".sort-indicator").textContent = active ? (direction === 1 ? " ▲" : " ▼") : " ↕";
    });
}

async function saveParticipantValidation(participant, checkbox) {
    const label = checkbox.closest("label").querySelector("span");
    const status = checkbox.closest("td").querySelector(".validation-status");
    checkbox.disabled = true;
    status.textContent = "Saving...";
    try {
        const formData = new FormData();
        formData.append("validated", String(checkbox.checked));
        const response = await fetch(`/api/admin/participant/${encodeURIComponent(participant.participant_code)}/validation`, {
            method: "POST", body: formData
        });
        if (!response.ok) throw new Error("Could not save validation. Please retry; the server may need a restart.");
        const result = await response.json();
        participant.admin_validated = result.admin_validated;
        checkbox.checked = result.admin_validated;
        label.textContent = result.admin_validated ? "Validated" : "Not validated";
        status.textContent = "Saved";
        if (participantSort.key === "admin_validated") sortParticipantRows();
    } catch (error) {
        checkbox.checked = Boolean(participant.admin_validated);
        status.textContent = error.message;
    } finally {
        checkbox.disabled = false;
    }
}


document.querySelectorAll(".participant-sort-btn").forEach(button => {
    button.addEventListener("click", () => {
        const key = button.dataset.sort;
        participantSort = {key, direction: participantSort.key === key ? -participantSort.direction : 1};
        sortParticipantRows();
    });
});

async function loadParticipantDetail(participantCode, toggle = false) {
    if (toggle && currentParticipantCode === participantCode) {
        closeParticipantDetail();
        return;
    }
    const row = Array.from(participantsTableBody.children).find(row => row.dataset.participantCode === participantCode);
    if (!row) return;
    closeParticipantDetail();
    currentParticipantCode = participantCode;
    const request = ++detailRequest;
    const detailRow = document.createElement("tr");
    detailRow.className = "participant-detail-row";
    const cell = document.createElement("td");
    cell.colSpan = 10;
    detailRow.appendChild(cell);
    cell.appendChild(participantDetailSection);
    row.after(detailRow);
    participantDetailSection.classList.remove("hidden");
    const button = row.querySelector(".participant-view-btn");
    button.textContent = "Hide";
    button.setAttribute("aria-expanded", "true");
    participantDetailInfo.textContent = "Loading participant details...";
    fullRetakeBtn.disabled = true;
    fullRetakeStatus.textContent = "";
    bulkRetakeStatus.textContent = "";
    selectAllRecordings.checked = false;
    selectAllRecordings.indeterminate = false;
    selectAllRecordings.disabled = true;
    bulkRetakeBtn.disabled = true;
    bulkRetakeCount.textContent = "0 selected";
    showFullRetakeBackup(null);
    missingSentencesEl.textContent = "";
    recordingsTableBody.replaceChildren();
    try {
        const response = await fetch(`/api/admin/participant/${participantCode}`);

        if (!response.ok) {
            throw new Error("Failed to load participant detail.");
        }

        const data = await response.json();
        if (request !== detailRequest) return;

        participantDetailSection.classList.remove("hidden");

        participantDetailInfo.innerHTML = `
            <p><strong>Participant Code:</strong> ${data.participant.participant_code}</p>
            <p><strong>Age Group:</strong> ${data.participant.age_group}</p>
            <p><strong>Gender:</strong> ${data.participant.gender}</p>
            <p><strong>Accent / Variety:</strong> ${formatIndonesianAccent(data.participant.indonesian_accent)}</p>
            <p><strong>Public Speaking Ability (X):</strong> ${speakingAbilityLabels[data.participant.public_speaking_ability] || "Not provided"}</p>
            <p><strong>Communication Style (Y):</strong> ${communicationStyleLabels[data.participant.communication_style] || "Not provided"}</p>
            <p><strong>Completed:</strong> ${data.completed} / ${data.total}</p>
            <p><strong>Total Duration:</strong> ${formatDuration(data.total_duration_seconds)}</p>
        `;

        if (data.missing_sentence_ids.length === 0) {
            missingSentencesEl.textContent = "No missing sentences.";
        } else {
            missingSentencesEl.textContent = data.missing_sentence_ids.join(", ");
        }

        renderRecordingsTable(data.recordings);
        fullRetakeBtn.disabled = fullRetakesInProgress.has(participantCode) || data.recordings.length === 0;
        showFullRetakeBackup(data.latest_backup);
        if (fullRetakesInProgress.has(participantCode)) fullRetakeStatus.textContent = "Backup and full retake request in progress...";



    } catch (error) {
        console.error(error);
        if (request === detailRequest) participantDetailInfo.textContent = error.message;
    }
}


function renderRecordingsTable(recordings) {
    clearPreviews();
    recordingsTableBody.innerHTML = "";

    if (!recordings || recordings.length === 0) {
        recordingsTableBody.innerHTML = `
            <tr>
                <td colspan="5">No recordings found.</td>
            </tr>
        `;
        return;
    }

    recordings.forEach(r => {
        const row = document.createElement("tr");

        const selectId = `quality-${r.sentence_id}`;
        const noteId = `note-${r.sentence_id}`;

        row.innerHTML = `
            <td class="recording-select-cell">
                <input type="checkbox" class="recording-retake-checkbox"
                    data-sentence-id="${escapeHtml(r.sentence_id)}"
                    aria-label="Select ${escapeHtml(r.sentence_id)} for retake"
                    ${r.quality_status === "retake_requested" ? "disabled" : ""}>
            </td>
            <td class="recording-sentence">
                <strong>${escapeHtml(r.sentence_id)}</strong>
                <p>${escapeHtml(r.sentence_text)}</p>
                <small>${escapeHtml(r.recording_time)} · ${formatDuration(r.duration_seconds)}</small>
            </td>
            <td class="recording-preview-cell">
                ${r.quality_status === "retake_requested" ? '<p>Awaiting retake</p>' : `
                <video class="recording-preview" controls playsinline preload="none"
                    aria-label="Recording ${escapeHtml(r.sentence_id)}"
                    data-src="/api/admin/recording/video/${encodeURIComponent(r.participant_code)}/${encodeURIComponent(r.sentence_id)}?v=${encodeURIComponent(r.recording_version || r.recording_time || "")}#t=0.1"></video>
                <button type="button" class="inline-play-btn">Play</button>
                <p class="playback-status" role="status"></p>`}
            </td>
            <td class="recording-quality-cell">
                <select id="${selectId}">
                    ${qualityOption("unchecked", r.quality_status)}
                    ${qualityOption("good", r.quality_status)}
                    ${qualityOption("bad_audio", r.quality_status)}
                    ${qualityOption("bad_video", r.quality_status)}
                    ${qualityOption("too_short", r.quality_status)}
                    ${qualityOption("too_long", r.quality_status)}
                    ${qualityOption("wrong_sentence", r.quality_status)}
                    ${qualityOption("background_noise", r.quality_status)}
                    ${qualityOption("face_not_visible", r.quality_status)}
                    ${qualityOption("retake_requested", r.quality_status)}
                    ${qualityOption("other", r.quality_status)}
                </select>
                <input
                    id="${noteId}"
                    type="text"
                    value="${escapeHtml(r.quality_note || "")}"
                    placeholder="Optional note"
                >
            </td>
            <td class="recording-actions-cell">
                <button onclick="updateQuality('${r.participant_code || ""}', '${r.sentence_id}', '${selectId}', '${noteId}')">
                    Save
                </button>
                <button class="warning-btn" onclick="requestRetake('${r.participant_code || ""}', '${r.sentence_id}')">
                    Request Retake
                </button>
            </td>
        `;

        row.dataset.participantCode = r.participant_code;

        recordingsTableBody.appendChild(row);
        row.querySelector(".recording-retake-checkbox").addEventListener("change", updateBulkRetakeSelection);
        const video = row.querySelector("video");
        if (video) {
            const playButton = row.querySelector(".inline-play-btn");
            const message = row.querySelector(".playback-status");
            const load = () => {
                if (!video.getAttribute("src")) {
                    video.preload = "metadata";
                    video.src = video.dataset.src;
                    video.load();
                }
            };
            playButton.addEventListener("click", async () => {
                message.textContent = "";
                load();
                if (!video.paused) { video.pause(); return; }
                try { await video.play(); }
                catch (error) { message.textContent = "Unable to play this recording. Try again or refresh."; }
            });
            video.addEventListener("play", () => {
                recordingsTableBody.querySelectorAll("video").forEach(other => { if (other !== video) other.pause(); });
                playButton.textContent = "Pause";
            });
            video.addEventListener("pause", () => { playButton.textContent = "Play"; });
            video.addEventListener("ended", () => { playButton.textContent = "Play"; });
            video.addEventListener("error", () => { message.textContent = "Video unavailable. Refresh to check its status."; });
            if ("IntersectionObserver" in window) {
                if (!previewObserver) previewObserver = new IntersectionObserver((entries, observer) => {
                    entries.forEach(entry => {
                        if (entry.isIntersecting) {
                            const preview = entry.target;
                            if (!preview.getAttribute("src")) {
                                preview.preload = "metadata";
                                preview.src = preview.dataset.src;
                                preview.load();
                            }
                            observer.unobserve(preview);
                        }
                    });
                }, { rootMargin: "200px" });
                previewObserver.observe(video);
            } else { load(); }
        }
    });
    updateBulkRetakeSelection();
}


function qualityOption(value, currentValue) {
    const selected = value === currentValue ? "selected" : "";
    return `<option value="${value}" ${selected}>${value}</option>`;
}


async function updateQualityFromDetail(participantCode, sentenceId, selectId, noteId) {
    const qualityStatus = document.getElementById(selectId).value;
    const qualityNote = document.getElementById(noteId).value;

    const formData = new FormData();
    formData.append("participant_code", participantCode);
    formData.append("sentence_id", sentenceId);
    formData.append("quality_status", qualityStatus);
    formData.append("quality_note", qualityNote);

    const response = await fetch("/api/admin/recording/quality", {
        method: "POST",
        body: formData
    });

    if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to update quality.");
    }

    return await response.json();
}


async function updateQuality(participantCode, sentenceId, selectId, noteId) {
    if (!participantCode) {
        const codeText = participantDetailInfo.querySelector("p strong");
    }

    const visibleCodeMatch = participantDetailInfo.innerText.match(/Participant Code:\s*(P\d+)/);
    const realParticipantCode = participantCode || (visibleCodeMatch ? visibleCodeMatch[1] : "");

    if (!realParticipantCode) {
        alert("Participant code not found.");
        return;
    }

    try {
        await updateQualityFromDetail(
            realParticipantCode,
            sentenceId,
            selectId,
            noteId
        );

        alert(`Quality updated for ${realParticipantCode} ${sentenceId}.`);
        if (document.getElementById(selectId).value === "retake_requested") await loadAdminSummary();

    } catch (error) {
        console.error(error);
        alert(error.message);
    }
}

async function requestRetake(participantCode, sentenceId) {
    const realParticipantCode = participantCode || currentParticipantCode;

    if (!realParticipantCode) {
        alert("Participant code not found.");
        return;
    }

    const confirmed = confirm(
        `Request retake for ${realParticipantCode} ${sentenceId}? ` +
        "The participant will see this sentence again when continuing the session."
    );

    if (!confirmed) {
        return;
    }

    const formData = new FormData();
    formData.append("participant_code", realParticipantCode);
    formData.append("sentence_id", sentenceId);
    formData.append("quality_status", "retake_requested");
    formData.append("quality_note", "Retake requested by admin.");

    try {
        const response = await fetch("/api/admin/recording/quality", {
            method: "POST",
            body: formData
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to request retake.");
        }

        alert(`Retake requested for ${realParticipantCode} ${sentenceId}.`);
        await loadAdminSummary();

    } catch (error) {
        console.error(error);
        alert(error.message);
    }
}

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
}

showDashboardBtn.addEventListener("click", function() {
    showAdminPage("dashboard");
});

showCredentialsBtn.addEventListener("click", function() {
    showAdminPage("credentials");
});

generateCredentialBtn.addEventListener("click", generateCredential);

window.addEventListener("load", function() {
    const initialPage = new URLSearchParams(window.location.search).get("page") === "credentials"
        ? "credentials" : "dashboard";
    showAdminPage(initialPage, false);
});
