let participantCode = null;
let sentences = [];
let currentIndex = 0;

let totalSentenceCount = 0;
let alreadyCompletedCount = 0;

let mediaStream = null;
let mediaRecorder = null;
let recordedChunks = [];
let reviewObjectUrl = null;

const sessionLoadingSection = document.getElementById("session-loading-section");
const sessionLoadingMessage = document.getElementById("session-loading-message");
const retrySessionBtn = document.getElementById("retry-session-btn");
const participantSection = document.getElementById("participant-section");
const recordingSection = document.getElementById("recording-section");
const finishedSection = document.getElementById("finished-section");

const participantForm = document.getElementById("participant-form");

const participantCodeDisplay = document.getElementById("participant-code-display");
const progressText = document.getElementById("progress-text");
const progressFill = document.getElementById("progress-fill");

const sentenceIdElement = document.getElementById("sentence-id");
const sentenceTextElement = document.getElementById("sentence-text");

const preview = document.getElementById("preview");

const mainRecordingBtn = document.getElementById("main-recording-btn");
const saveBtn = document.getElementById("save-btn");
const recordAgainBtn = document.getElementById("record-again-btn");
const reviewVideo = document.getElementById("review-video");

const formParticipantCodeDisplay = document.getElementById("form-participant-code-display");
const participantStatus = document.getElementById("participant-status");
const startSessionBtn = document.getElementById("start-session-btn");
const ageGroupInput = document.getElementById("age_group");
const genderInput = document.getElementById("gender");
const indonesianAccentInput = document.getElementById("indonesian_accent");

let currentBlob = null;
let recordingState = "idle"; 

const statusMessage = document.getElementById("status-message");
let currentParticipantInfo = null;

reviewVideo.addEventListener("error", function() {
    if (!reviewVideo.getAttribute("src")) return;
    setStatus("This browser could not play the recording preview. Please choose Record Again or try a current version of Chrome, Edge, or Safari.", true);
});


// ==========================
// Utility
// ==========================

function setStatus(message, isError = false) {
    statusMessage.textContent = message;
    statusMessage.style.color = isError ? "crimson" : "#333";
}

function updateProgress() {
    const completedNow = alreadyCompletedCount + currentIndex;
    const total = totalSentenceCount;

    const percentage = total > 0 ? (completedNow / total) * 100 : 0;

    progressText.textContent = `${completedNow} / ${total}`;
    progressFill.style.width = `${percentage}%`;
}

function showCurrentSentence() {
    if (currentIndex >= sentences.length) {
        finishSession();
        return;
    }

    const sentence = sentences[currentIndex];

    sentenceIdElement.textContent = sentence.sentence_id;
    sentenceTextElement.textContent = sentence.sentence_text;

    updateProgress();

    currentBlob = null;
    recordedChunks = [];
    recordingState = "idle";

    clearReviewVideo();
    preview.classList.remove("hidden");

    mainRecordingBtn.classList.remove("hidden");
    mainRecordingBtn.classList.remove("recording-active");
    mainRecordingBtn.textContent = "Start Recording";
    mainRecordingBtn.disabled = false;

    saveBtn.classList.add("hidden");
    recordAgainBtn.classList.add("hidden");

    setStatus("Click the button when you are ready.");
}

function finishSession() {
    recordingSection.classList.add("hidden");
    finishedSection.classList.remove("hidden");

    if (mediaStream) {
        mediaStream.getTracks().forEach(track => track.stop());
    }
}


// ==========================
// API calls
// ==========================

async function waitForRecordingJob(jobId) {
    let delay = 2000;
    while (true) {
        const response = await fetch(`/api/recordings/jobs/${jobId}`, { cache: "no-store" });
        if (!response.ok) {
            throw new Error("Unable to check your saved upload. Retry Save & Continue or refresh to resume.");
        }
        const job = await response.json();
        if (job.status === "complete") return job.result;
        if (job.status === "failed") throw new Error(job.error);
        const message = job.status === "queued"
            ? "Upload saved. Waiting for processing; please keep this page open."
            : "Upload saved. Processing your recording...";
        setStatus(message);
        participantStatus.textContent = message;
        sessionLoadingMessage.textContent = message;
        await new Promise(resolve => setTimeout(resolve, delay + Math.random() * 1000));
        delay = Math.min(10000, delay * 1.5);
    }
}

async function getParticipantSession(code) {
    while (true) {
        const response = await fetch(`/api/session/${code}`, { cache: "no-store" });
        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to load participant session.");
        }
        const data = await response.json();
        if (!data.pending_jobs || data.pending_jobs.length === 0) return data;
        for (const jobId of data.pending_jobs) await waitForRecordingJob(jobId);
    }
}

async function getCurrentParticipant() {
    const response = await fetch("/api/participant/me");

    if (response.status === 401) {
        window.location.href = "/";
        return null;
    }

    if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to load participant login.");
    }

    return await response.json();
}

async function loadSentences() {
    const response = await fetch("/api/sentences");

    if (!response.ok) {
        throw new Error("Failed to load sentences.");
    }

    const data = await response.json();
    sentences = data.sentences;

    if (!sentences || sentences.length === 0) {
        throw new Error("No sentences found.");
    }
}


async function uploadRecording(blob, sentence) {
    const formData = new FormData();

    formData.append("participant_code", participantCode);
    formData.append("sentence_id", sentence.sentence_id);
    formData.append("file", blob, `${participantCode}_${sentence.sentence_id}.${recordingFileExtension(blob)}`);

    const response = await fetch("/api/recordings/upload", {
        method: "POST",
        body: formData
    });

    if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Upload failed.");
    }

    const data = await response.json();
    return data.job_id ? await waitForRecordingJob(data.job_id) : data;
}

async function startRecordingSession(code, completedSentenceIds = []) {
    participantCode = code;

    await loadSentences();

    totalSentenceCount = sentences.length;
    alreadyCompletedCount = completedSentenceIds.length;

    const completedSet = new Set(completedSentenceIds);

    sentences = sentences.filter(sentence => {
        return !completedSet.has(sentence.sentence_id);
    });

    if (sentences.length === 0) {
        participantSection.classList.add("hidden");
        recordingSection.classList.add("hidden");
        finishedSection.classList.remove("hidden");
        return;
    }

    participantStatus.textContent = "Allow camera and microphone access in your browser to continue.";
    sessionLoadingMessage.textContent = participantStatus.textContent;
    await startCameraAndMic();

    participantCodeDisplay.textContent = participantCode;

    participantSection.classList.add("hidden");
    recordingSection.classList.remove("hidden");

    currentIndex = 0;
    showCurrentSentence();
    sentenceTextElement.closest(".sentence-box").scrollIntoView({ block: "start" });
}

// ==========================
// Camera and microphone
// ==========================

async function startCameraAndMic() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error("Camera and microphone access is unavailable. Open this site using HTTPS in a supported browser.");
    }
    mediaStream = await navigator.mediaDevices.getUserMedia({
        video: {
            width: { ideal: 1280 },
            height: { ideal: 720 },
            facingMode: "user"
        },
        audio: {
            echoCancellation: false,
            noiseSuppression: false,
            autoGainControl: false
        }
    });

    preview.srcObject = mediaStream;
}


// ==========================
// Recording
// ==========================

function getSupportedMimeType() {
    const types = [
        "video/webm;codecs=vp9,opus",
        "video/webm;codecs=vp8,opus",
        "video/webm",
        "video/mp4;codecs=avc1.42E01E,mp4a.40.2",
        "video/mp4"
    ];

    for (const type of types) {
        if (MediaRecorder.isTypeSupported(type)) {
            return type;
        }
    }

    return "";
}

function clearReviewVideo() {
    reviewVideo.pause();
    reviewVideo.removeAttribute("src");
    reviewVideo.load();
    reviewVideo.classList.add("hidden");
    if (reviewObjectUrl) {
        URL.revokeObjectURL(reviewObjectUrl);
        reviewObjectUrl = null;
    }
}

function recordingFileExtension(blob) {
    return (blob.type || "").toLowerCase().includes("mp4") ? "mp4" : "webm";
}

async function startCountdown() {
    recordingState = "countdown";
    mainRecordingBtn.disabled = true;

    setStatus("Recording starts in 0.5 seconds...");
    mainRecordingBtn.textContent = "Starting...";
    await new Promise(resolve => setTimeout(resolve, 500));

    try {
        beginRecording();
    } catch (error) {
        recordingState = "idle";
        mainRecordingBtn.disabled = false;
        mainRecordingBtn.textContent = "Start Recording";
        mainRecordingBtn.classList.remove("recording-active");
        setStatus(`Could not start recording: ${error.message}`, true);
    }
}


function beginRecording() {
    recordedChunks = [];
    currentBlob = null;

    const mimeType = getSupportedMimeType();
    const options = mimeType ? { mimeType } : {};

    mediaRecorder = new MediaRecorder(mediaStream, options);

    mediaRecorder.ondataavailable = function(event) {
        if (event.data && event.data.size > 0) {
            recordedChunks.push(event.data);
        }
    };

    mediaRecorder.onstop = function() {
        const recordedMimeType = mediaRecorder.mimeType || mimeType || recordedChunks[0]?.type || "video/webm";
        currentBlob = new Blob(recordedChunks, { type: recordedMimeType });

        if (currentBlob.size === 0) {
            recordingState = "idle";
            mainRecordingBtn.classList.remove("hidden", "recording-active");
            mainRecordingBtn.textContent = "Start Recording";
            setStatus("The browser produced an empty recording. Please record again.", true);
            return;
        }

        clearReviewVideo();
        reviewObjectUrl = URL.createObjectURL(currentBlob);
        reviewVideo.src = reviewObjectUrl;
        preview.classList.add("hidden");
        reviewVideo.classList.remove("hidden");
        reviewVideo.load();

        recordingState = "review";

        mainRecordingBtn.classList.add("hidden");
        saveBtn.classList.remove("hidden");
        recordAgainBtn.classList.remove("hidden");

        setStatus("Review the recording. Save it or record again.");
    };

    mediaRecorder.start();

    recordingState = "recording";

    mainRecordingBtn.disabled = false;
    mainRecordingBtn.textContent = "Stop Recording";
    mainRecordingBtn.classList.add("recording-active");

    setStatus("Recording... Please read the sentence naturally.");
}


function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== "inactive") {
        mediaRecorder.stop();
    }
}


async function saveAndContinue() {
    if (!currentBlob) {
        setStatus("No recording found.", true);
        return;
    }

    recordingState = "uploading";

    saveBtn.disabled = true;
    recordAgainBtn.disabled = true;

    setStatus("Uploading and processing recording. Please wait...");

    const sentence = sentences[currentIndex];

    try {
        await uploadRecording(currentBlob, sentence);

        currentIndex += 1;

        saveBtn.disabled = false;
        recordAgainBtn.disabled = false;

        setStatus("Recording saved successfully.");

        showCurrentSentence();

    } catch (error) {
        console.error(error);

        saveBtn.disabled = false;
        recordAgainBtn.disabled = false;

        recordingState = "review";
        setStatus(`Error: ${error.message}`, true);
    }
}


function recordAgain() {
    currentBlob = null;
    recordedChunks = [];

    clearReviewVideo();
    preview.classList.remove("hidden");

    saveBtn.classList.add("hidden");
    recordAgainBtn.classList.add("hidden");

    mainRecordingBtn.classList.remove("hidden");
    mainRecordingBtn.classList.remove("recording-active");
    mainRecordingBtn.textContent = "Start Recording";
    mainRecordingBtn.disabled = false;

    recordingState = "idle";

    setStatus("Ready to record this sentence again.");
}

async function initializeParticipantPage() {
    participantSection.classList.add("hidden");
    sessionLoadingSection.classList.remove("hidden");
    sessionLoadingMessage.textContent = "Loading your session...";
    retrySessionBtn.classList.add("hidden");
    try {
        currentParticipantInfo = await getCurrentParticipant();
        if (!currentParticipantInfo) {
            return;
        }

        participantCode = currentParticipantInfo.participant_code;
        formParticipantCodeDisplay.textContent = participantCode;
        if (currentParticipantInfo.profile) {
            for (const [key, value] of Object.entries(currentParticipantInfo.profile)) {
                const input = document.getElementById(key);
                if (input) input.value = value;
            }
            // Existing demographics remain associated with previous recordings.
            ageGroupInput.disabled = true;
            genderInput.disabled = true;
            indonesianAccentInput.disabled = true;
        }


        if (currentParticipantInfo.profile_complete) {
            const sessionData = await getParticipantSession(participantCode);
            await startRecordingSession(
                sessionData.participant_code,
                sessionData.completed_sentence_ids
            );
        } else {
            participantSection.classList.remove("hidden");
            recordingSection.classList.add("hidden");
            finishedSection.classList.add("hidden");
            setStatus("Please complete participant information, then click Start Recording.");
        }

        sessionLoadingSection.classList.add("hidden");
    } catch (error) {
        console.error(error);
        sessionLoadingMessage.textContent = error.message;
        retrySessionBtn.classList.remove("hidden");
    }
}

// ==========================
// Event listeners
// ==========================

participantForm.addEventListener("submit", async function(event) {
    event.preventDefault();
    if (!participantForm.reportValidity() || startSessionBtn.disabled) return;
    startSessionBtn.disabled = true;
    startSessionBtn.textContent = "Starting...";
    participantStatus.textContent = "Saving participant information...";
    try {
        const formData = new FormData();
        formData.append("age_group", ageGroupInput.value);
        formData.append("gender", genderInput.value);
        formData.append("public_speaking_ability", document.getElementById("public_speaking_ability").value);
        formData.append("communication_style", document.getElementById("communication_style").value);
        formData.append("indonesian_accent", indonesianAccentInput.value);
        const response = await fetch("/api/participant/register", {
            method: "POST",
            body: formData
        });
        const data = await response.json();
        if (!response.ok) {
            throw new Error(data.detail || "Failed to save participant information.");
        }
        const sessionData = await getParticipantSession(data.participant_code);
        await startRecordingSession(sessionData.participant_code, sessionData.completed_sentence_ids);
    } catch (error) {
        console.error(error);
        participantStatus.textContent = error.message;
        alert(error.message);
    } finally {
        startSessionBtn.disabled = false;
        startSessionBtn.textContent = "Start Recording";
    }
});

mainRecordingBtn.addEventListener("click", function() {
    if (recordingState === "idle") {
        startCountdown();
    } else if (recordingState === "recording") {
        stopRecording();
    }
});

saveBtn.addEventListener("click", saveAndContinue);
recordAgainBtn.addEventListener("click", recordAgain);

retrySessionBtn.addEventListener("click", initializeParticipantPage);
window.addEventListener("load", initializeParticipantPage);
