const icfForm = document.getElementById("icf-form");
const icfResult = document.getElementById("icf-result");
const signaturePad = document.getElementById("signature-pad");
const clearSignatureBtn = document.getElementById("clear-signature-btn");

const participantCodeEl = document.getElementById("icf_participant_code");
const dateEl = document.getElementById("icf_date");
const ageGroupEl = document.getElementById("icf_age_group");
const genderEl = document.getElementById("icf_gender");
const accentEl = document.getElementById("icf_accent");

const accentLabels = {
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

const params = new URLSearchParams(window.location.search);
const context = signaturePad.getContext("2d");
let activePad = null;
let hasSignature = false;

function getRequiredInfo() {
    return {
        ageGroup: params.get("age_group") || "",
        gender: params.get("gender") || "",
        indonesianAccent: params.get("indonesian_accent") || ""
    };
}

function requiredInfoComplete() {
    const info = getRequiredInfo();
    return Boolean(info.ageGroup && info.gender && info.indonesianAccent);
}

function pointerPosition(event, pad) {
    const rect = pad.getBoundingClientRect();
    const pointer = event.touches ? event.touches[0] : event;
    return {
        x: (pointer.clientX - rect.left) * (pad.width / rect.width),
        y: (pointer.clientY - rect.top) * (pad.height / rect.height)
    };
}

function startDrawing(event, pad, padContext, markSigned) {
    event.preventDefault();
    activePad = { pad, context: padContext };
    markSigned();
    const pos = pointerPosition(event, pad);
    padContext.beginPath();
    padContext.moveTo(pos.x, pos.y);
}

function drawSignature(event) {
    if (!activePad) {
        return;
    }

    event.preventDefault();
    const pos = pointerPosition(event, activePad.pad);
    activePad.context.lineTo(pos.x, pos.y);
    activePad.context.stroke();
}

function stopDrawing() {
    activePad = null;
}

function clearSignature() {
    context.clearRect(0, 0, signaturePad.width, signaturePad.height);
    hasSignature = false;
}

async function loadCurrentParticipantForIcf() {
    const response = await fetch("/api/participant/me");

    if (response.status === 401) {
        window.location.href = "/";
        return;
    }

    if (!response.ok) {
        alert("Failed to load participant login.");
        return;
    }

    const participant = await response.json();
    const info = getRequiredInfo();

    participantCodeEl.textContent = participant.participant_code;
    ageGroupEl.textContent = info.ageGroup || "-";
    genderEl.textContent = info.gender || "-";
    accentEl.textContent = accentLabels[info.indonesianAccent] || info.indonesianAccent || "-";

    if (!requiredInfoComplete() && !participant.profile_complete) {
        icfForm.classList.add("hidden");
        icfResult.innerHTML = `
            <p><strong>Participant information is incomplete.</strong></p>
            <p><a href="/record">Return to Participant Information</a></p>
        `;
        icfResult.classList.remove("hidden");
    }
}

context.lineWidth = 3;
context.lineCap = "round";
context.strokeStyle = "#111";

signaturePad.addEventListener("mousedown", function(event) {
    startDrawing(event, signaturePad, context, function() {
        hasSignature = true;
    });
});
signaturePad.addEventListener("mousemove", drawSignature);
signaturePad.addEventListener("mouseup", stopDrawing);
signaturePad.addEventListener("mouseleave", stopDrawing);
signaturePad.addEventListener("touchstart", function(event) {
    startDrawing(event, signaturePad, context, function() {
        hasSignature = true;
    });
});
signaturePad.addEventListener("touchmove", drawSignature);
signaturePad.addEventListener("touchend", stopDrawing);

clearSignatureBtn.addEventListener("click", clearSignature);

icfForm.addEventListener("submit", async function(event) {
    event.preventDefault();

    if (!requiredInfoComplete()) {
        alert("Please complete participant information before opening the ICF.");
        return;
    }

    if (!hasSignature) {
        alert("Please provide the participant signature before saving.");
        return;
    }

    const requiredChecks = [
        document.getElementById("icf_check_retention").checked,
        document.getElementById("icf_check_deidentification").checked,
        document.getElementById("icf_check_future_use").checked
    ];

    if (!requiredChecks.every(Boolean)) {
        alert("Please check all required consent boxes before saving.");
        return;
    }

    const info = getRequiredInfo();
    const formData = new FormData();
    formData.append("age_group", info.ageGroup);
    formData.append("gender", info.gender);
    formData.append("indonesian_accent", info.indonesianAccent);
    formData.append("signature_data", signaturePad.toDataURL("image/png"));
    formData.append("consent_check_retention", "true");
    formData.append("consent_check_deidentification", "true");
    formData.append("consent_check_future_use", "true");

    try {
        const response = await fetch("/api/participant/icf/sign", {
            method: "POST",
            body: formData
        });

        if (!response.ok) {
            const errorData = await response.json();
            throw new Error(errorData.detail || "Failed to save signed ICF.");
        }

        const data = await response.json();
        dateEl.textContent = data.consent_timestamp.split(" ")[0];
        icfResult.innerHTML = `
            <p><strong>Signed ICF PDF saved.</strong></p>
            <p><a href="/api/participant/consent-pdf" target="_blank" download>Download Signed ICF PDF</a></p>
            <p><a href="/record">Continue to Recording</a></p>
        `;
        icfResult.classList.remove("hidden");

        window.location.href = data.redirect_url || "/record";

    } catch (error) {
        console.error(error);
        alert(error.message);
    }
});

window.addEventListener("load", loadCurrentParticipantForIcf);
