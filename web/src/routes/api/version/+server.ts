import { pipelineStatus } from '#lib/server/detail.ts';
import type { RequestHandler } from './$types';

// Polled by the layout: tells the page when the pipeline has written something.
export const GET: RequestHandler = () => Response.json(pipelineStatus());
