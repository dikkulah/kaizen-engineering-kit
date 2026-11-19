/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: 'no-circular-dependencies',
      severity: 'warn',
      comment: 'ADP (Acyclic Dependencies Principle): Modules should form a strict DAG.',
      from: {},
      to: { circular: true }
    },
    {
      name: 'utils-pure-no-ui-layer',
      severity: 'error',
      comment: 'Clean Architecture: Low-level utils/types must never import views, modules, or components.',
      from: { path: '^src/(utils|types)/' },
      to: { path: '^src/(views|modules|router|components)/' }
    },
    {
      name: 'api-layer-isolation',
      severity: 'warn',
      comment: 'Clean Architecture: API transport layer should not depend on UI views or components.',
      from: { path: '^src/api/' },
      to: { path: '^src/(views|components)/' }
    }
  ],
  options: {
    doNotFollow: { path: 'node_modules' },
    exclude: { path: '(\\.spec\\.|\\.test\\.|__tests__)' },
    tsPreCompilationDeps: true
  }
};
