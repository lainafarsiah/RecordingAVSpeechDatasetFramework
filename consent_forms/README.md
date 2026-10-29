# Consent form assets

This public repository does not contain study-specific consent documents,
signatures, investigator identities, contact information, or retention terms.

Before collecting participants, obtain the required ethics approval and provide:

- `consent.pdf`: the approved consent template;
- `explainer_signature.png`: an authorized signature image if your approved
  procedure requires the PDF overlay feature; and
- matching participant-facing consent language in `static/icf_sign.html`.

Configure alternate paths with `CONSENT_TEXT_PATH`,
`EXPLAINER_SIGNATURE_PATH`, and `CONSENT_VERSION`.

The included recording flow can register participants without the optional PDF
consent workflow. Deployers are responsible for enforcing the consent procedure
required by their institution and jurisdiction before data collection.
