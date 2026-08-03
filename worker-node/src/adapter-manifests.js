const commonAccountSchema = {
  type: 'object',
  properties: {
    headless: { type: 'boolean', title: '无头模式', default: true },
    browserMode: {
      type: 'string',
      title: '浏览器模式',
      enum: ['persistent', 'system_chrome'],
      default: 'persistent',
    },
  },
  additionalProperties: true,
};

function manifest(adapterId, title, options = {}) {
  return {
    adapterId,
    version: '1.0.0',
    title,
    capabilities: ['checkin'],
    siteConfigSchema: { type: 'object', properties: {}, additionalProperties: true },
    accountConfigSchema: options.schema || commonAccountSchema,
    uiSchema: options.uiSchema || {},
    secretFields: options.secretFields || [],
    defaults: options.defaults || {},
    conditionalRules: options.conditionalRules || [],
  };
}

export const adapterManifests = [
  manifest('api', 'HTTP API', {
    schema: {
      type: 'object',
      required: ['checkinUrl'],
      properties: {
        checkinUrl: { type: 'string', format: 'uri', title: '签到地址' },
        method: { type: 'string', enum: ['GET', 'POST'], default: 'POST' },
        headers: {
          type: 'object',
          default: {},
          additionalProperties: true,
          properties: {
            Authorization: { type: 'string', title: 'Authorization' },
            'X-Api-Key': { type: 'string', title: 'X-Api-Key' },
          },
        },
        body: {
          type: 'object',
          default: {},
          additionalProperties: true,
          properties: {
            token: { type: 'string', title: 'Token' },
          },
        },
        successKeyword: { type: 'string', default: 'success' },
        timeoutMs: { type: 'integer', minimum: 1000, maximum: 120000, default: 30000 },
      },
      additionalProperties: true,
    },
    secretFields: ['headers.Authorization', 'headers.X-Api-Key', 'body.token'],
  }),
  manifest('cookie', 'Cookie HTTP', {
    schema: {
      type: 'object',
      required: ['cookie'],
      properties: {
        cookie: { type: 'string', title: 'Cookie' },
        checkinUrl: { type: 'string', format: 'uri' },
        method: { type: 'string', enum: ['GET', 'POST'], default: 'POST' },
        headers: {
          type: 'object',
          default: {},
          additionalProperties: true,
          properties: {
            Authorization: { type: 'string', title: 'Authorization' },
            'X-Api-Key': { type: 'string', title: 'X-Api-Key' },
          },
        },
        body: {
          type: 'object',
          default: {},
          additionalProperties: true,
          properties: {
            token: { type: 'string', title: 'Token' },
          },
        },
        successKeyword: { type: 'string', default: 'success' },
        timeoutMs: { type: 'integer', minimum: 1000, maximum: 120000, default: 30000 },
      },
      additionalProperties: true,
    },
    secretFields: ['cookie', 'headers.Authorization', 'headers.X-Api-Key', 'body.token'],
  }),
  manifest('playwright-generic', '通用 Playwright', {
    schema: {
      ...commonAccountSchema,
      properties: {
        ...commonAccountSchema.properties,
        cookie: { type: 'string', title: 'Cookie' },
        checkinButtonSelector: { type: 'string', title: '签到按钮选择器' },
        alreadyCheckedSelector: { type: 'string', title: '已签到选择器' },
        alreadyCheckedText: { type: 'string', title: '已签到文字', default: '已签' },
        waitSelector: { type: 'string', title: '页面就绪选择器' },
      },
    },
    secretFields: ['cookie'],
  }),
  manifest('xkool', 'XKool'),
  manifest('nodeloc', 'NodeLoc'),
  manifest('nodeseek', 'NodeSeek', {
    schema: {
      ...commonAccountSchema,
      properties: {
        ...commonAccountSchema.properties,
        signMode: { type: 'string', enum: ['chicken', 'luck'], default: 'chicken' },
      },
    },
  }),
  manifest('hundredxlabs', '100xLabs'),
  manifest('api123nhh', 'api.123nhh.com'),
  manifest('jianzhile', 'jianzhile.vip', {
    schema: {
      ...commonAccountSchema,
      properties: {
        ...commonAccountSchema.properties,
        captchaMaxAttempts: { type: 'integer', minimum: 1, maximum: 5, default: 2 },
      },
    },
  }),
  manifest('btschool', 'BTSchool'),
  manifest('52pojie', '吾爱破解'),
];
