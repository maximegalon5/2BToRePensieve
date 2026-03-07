# Entity Normalization: Before vs After

Before: 2026-03-06T18:50:25.825517
After:  2026-03-06T19:11:38.692467

## Entity Fragmentation

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| Total entities | 11769 | 9576 | -2193 |
| Unique names | 9576 | 9576 | +0 |
| Fragmented names | 1306 | 0 | -1306 |
| Fragmented entities | 3499 | 0 | -3499 |

### Top fragmented entities (before)

- **sats**: 16x as project, feature, system, assessment tool, program, framework, protocol, assessment, data_source, concept, data, Project, product, organization, Concept, tool
- **section 6**: 13x as project_section, section, code_section, document, project, concept, component, project_phase, event, artifact, document_section, place, part
- **section 5**: 12x as concept, place, project_section, event, section, project, document, component, code_section, project_phase, artifact, document_section
- **section 8**: 12x as code_section, place, project, project_section, concept, section, Code Section, project_component, document, component, version, part
- **msq**: 10x as data_source, tool, assessment, data, project, test, document, Concept, report, concept
- **foundations report**: 9x as document, Concept, project, artifact, Tool, concept, product, report, tool
- **client**: 9x as person, tool, entity, organization, variable, role, concept, project, data_structure
- **exampleorg**: 9x as Organization, project, concept, tool, application, place, platform, organization, brand
- **claude**: 9x as tool, AI model, concept, system, AI, LLM, Tool, person, AI assistant
- **magnesium**: 9x as tool, supplement, nutrient, mineral, concept, compound, Mineral, ingredient, micronutrient

## Entity Type Distribution

| Metric | Before | After |
|--------|--------|-------|
| Distinct types | 517 | 6 |

### After distribution

| Type | Count |
|------|-------|
| concept | 6306 |
| tool | 1447 |
| project | 760 |
| organization | 567 |
| person | 480 |
| content | 16 |

## Graph Connectivity

| Metric | Before | After | Change |
|--------|--------|-------|--------|
| entities | 11769 | 9576 | -2193 |
| relations | 12056 | 11514 | -542 |
| observations | 25047 | 25047 | +0 |
| avg_relations_per_entity | 1.02 | 1.2 | +0.18 |
| avg_observations_per_entity | 2.13 | 2.62 | +0.49 |

## Search Completeness

| Query | Before results | After results | Before entities | After entities |
|-------|---------------|--------------|----------------|---------------|
| ExampleOrg | 10 | 0 | 3 | 0 |
| SATS | 0 | 10 | 0 | 9 |
| User | 10 | 10 | 6 | 6 |
| ProductA | 0 | 0 | 0 | 0 |
| ProductB | 10 | 10 | 6 | 4 |
| what is ExampleOrg and what do I know about it | 0 | 0 | 0 | 0 |
| tell me about SATS | 0 | 10 | 0 | 9 |
| Python programming | 0 | 10 | 0 | 9 |
| React framework | 10 | 10 | 9 | 9 |
| PersonB | 10 | 10 | 6 | 5 |
