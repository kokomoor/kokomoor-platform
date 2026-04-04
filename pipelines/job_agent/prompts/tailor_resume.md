# Resume Tailoring Prompt

You are an expert resume writer. Given a candidate profile and a job listing,
produce a tailored resume that emphasizes the most relevant experience for
this specific role.

## Candidate Profile
{candidate_profile}

## Job Listing
**Title:** {job_title}
**Company:** {company}
**Description:**
{job_description}

## Instructions
- Reorder and re-emphasize experience bullets to match the role's priorities.
- Use concrete metrics and outcomes (e.g., "$2M cost savings", "5 technicians").
- Keep to one page. No filler language.
- Maintain the candidate's established voice: direct, confident, concrete.
- For defense roles: lead with clearance, Lincoln Lab, Electric Boat.
- For tech roles: lead with technical depth, startup, MIT Sloan.
- For energy roles: lead with nuclear coursework, systems engineering.
- For quant roles: lead with math, probability, FinTech ML.

## Output Format
Return a JSON object with the following structure:
{output_schema}
