#!/bin/bash

# AI Agent System Startup Script

set -e

echo "🚀 Starting AI Agent System..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is not installed. Please install Python 3.9 or higher."
    exit 1
fi

# Check if Node.js is installed
if ! command -v node &> /dev/null; then
    echo "❌ Node.js is not installed. Please install Node.js 18 or higher."
    exit 1
fi

# Setup backend (run from project root so module paths resolve correctly)
echo "📦 Setting up backend..."

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment
source .venv/bin/activate

# Install dependencies
echo "Installing Python dependencies..."
pip install -q -r requirements.txt

# Load .env if present
if [ -f ".env" ]; then
    export $(grep -v '^#' .env | grep -v '^VITE_' | xargs)
fi

# Start backend from project root so imports resolve correctly
echo "🌐 Starting backend server on http://localhost:8000"
uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Setup frontend
echo "📦 Setting up frontend..."

# Install dependencies if node_modules doesn't exist
if [ ! -d "node_modules" ]; then
    echo "Installing Node.js dependencies..."
    npm install
fi

# Build frontend
echo "🔨 Building frontend..."
npm run build

# Start frontend preview
echo "🎨 Starting frontend on http://localhost:4173"
npm run preview &
FRONTEND_PID=$!

echo ""
echo "✅ AI Agent System is running!"
echo ""
echo "📍 Backend: http://localhost:8000"
echo "📍 Frontend: http://localhost:4173"
echo "📍 API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers"
echo ""

# Wait for interrupt
trap "kill $BACKEND_PID $FRONTEND_PID; exit" INT
wait
